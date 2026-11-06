"""Land a branch into the base branch.

This is a human job and it runs from the main checkout only. gh refuses to
delete a branch that is checked out in a worktree, and the whole point of the
push guard is that a worktree never touches the base branch.

prefix+X l runs it from the branch session anyway, in a window whose folder
is the main checkout. See open_window. prefix+X m does the same with --merge.

Some repos must never talk to their remote, or have none. [repo].land = "merge"
or --merge takes the other road: one `git merge --no-ff` in the main checkout,
and nothing here touches the remote. See _land_merge.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path

from . import git, review, tmux, wt
from .config import WtxConfig
from .context import Ctx, session_name
from .proc import CommandError, capture, capture_code, is_dry_run, run, run_shell, say, warn, which

# The window prefix+X l opens in the branch's own session.
WINDOW = "land"


class LandError(Exception):
    pass


def merges_locally(cfg: WtxConfig, asked: bool) -> bool:
    """True when this land must stay off the remote, by the flag or the rule.

    The land and its clean up are two processes and both ask here. A clean up
    that gets it wrong pushes to a remote the land never touched.
    """
    return asked or cfg.repo.land == "merge"


def _gh(args: list[str], cwd: Path) -> tuple[int, str, str]:
    return capture_code(["gh", *args], cwd=cwd)


def pr_state(cwd: Path, branch: str) -> tuple[str, str]:
    """(state, url) of the branch's pull request, empty when there is none."""
    code, out, _ = _gh(["pr", "view", branch, "--json", "state,url"], cwd)
    if code != 0 or not out:
        return "", ""
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return "", ""
    return data.get("state", ""), data.get("url", "")


def _checks(ctx: Ctx, path: Path) -> None:
    commands = list(ctx.cfg.checks.lint) + list(ctx.cfg.checks.test)
    for cmd in commands:
        say(f"check: {cmd}")
        try:
            run_shell(cmd, cwd=path)
        except CommandError as exc:
            raise LandError(f"check failed: {cmd}") from exc


def _warn_on_review(ctx: Ctx, branch: str, path: Path) -> None:
    """Warn only. A review helps the human read, it does not gate a merge.

    ctx.session is the main checkout's here, so the name is built for the
    branch.
    """
    state = review.status(session_name(ctx.repo_name, branch), review.head(path))
    if state == "none":
        warn(f"no review for {branch}, see wtx review {branch}")
    elif state == "stale":
        warn(f"the review of {branch} is older than its last commit")


def cleanup(ctx: Ctx, branch: str, *, keep_branch: bool = False, remote: bool = True) -> None:
    """Remove the worktree and the branch, local and remote.

    With remote=False the origin is left alone. The local branch can then be
    the only copy of the work, so it goes only if the base really contains it.
    The job runs after the land, and a commit can arrive between the two.
    """
    if git.worktree_path_for(ctx.main, branch):
        try:
            wt.remove(ctx.main, branch)
        except wt.WtError as exc:
            warn(f"could not remove the worktree: {exc}")
    if keep_branch:
        return
    base = ctx.cfg.repo.base_branch
    # In a dry run the merge was only printed, so the base has nothing yet.
    if not remote and not is_dry_run() and not git.is_ancestor(ctx.main, branch, base):
        warn(f"{base} does not contain all of {branch}, the branch is kept")
        return
    run(["git", "branch", "-D", branch], cwd=ctx.main, check=False, quiet=True)
    if not remote:
        return
    run(
        ["git", "push", "origin", "--delete", branch],
        cwd=ctx.main,
        check=False,
        quiet=True,
    )


def land(
    ctx: Ctx,
    branch: str,
    *,
    local: bool = False,
    merge: bool = False,
    skip_checks: bool = False,
    keep_branch: bool = False,
    poll_seconds: int = 20,
    timeout_minutes: int = 30,
) -> None:
    cfg = ctx.cfg
    base = cfg.repo.base_branch
    merge = merges_locally(cfg, merge)

    if ctx.is_worktree:
        raise LandError(f"run this from the main checkout ({ctx.main}), not from a worktree")
    if branch in cfg.repo.protected_branches:
        raise LandError(f"{branch} is a protected branch, there is nothing to land")

    path = git.worktree_path_for(ctx.main, branch)
    if path is None:
        raise LandError(f"no worktree for {branch}")
    if not git.is_clean(path):
        raise LandError(f"{path} has uncommitted changes, commit or stash them first")
    _warn_on_review(ctx, branch, path)

    if merge:
        _land_merge(ctx, branch, base, path, skip_checks=skip_checks)
    else:
        _land_remote(
            ctx,
            branch,
            base,
            path,
            local=local,
            skip_checks=skip_checks,
            poll_seconds=poll_seconds,
            timeout_minutes=timeout_minutes,
        )
    if tmux.current_session() == session_name(ctx.repo_name, branch):
        _hand_off_cleanup(ctx, branch, keep_branch=keep_branch, merge=merge)
    else:
        cleanup(ctx, branch, keep_branch=keep_branch, remote=not merge)
    say(f"landed {branch} into {base}" + (", locally, nothing was pushed" if merge else ""))


def _land_remote(
    ctx: Ctx,
    branch: str,
    base: str,
    path: Path,
    *,
    local: bool,
    skip_checks: bool,
    poll_seconds: int,
    timeout_minutes: int,
) -> None:
    """The land that goes through origin: rebase, push, then a PR or a merge."""
    git.fetch(ctx.main)
    _refuse_a_branch_cut_from_elsewhere(ctx, branch, base)

    say(f"rebasing {branch} onto origin/{base}")
    try:
        run(["git", "rebase", f"origin/{base}"], cwd=path)
    except CommandError as exc:
        run(["git", "rebase", "--abort"], cwd=path, check=False, quiet=True)
        raise LandError(
            f"rebase onto origin/{base} has conflicts. Resolve them in {path} "
            f"(git rebase origin/{base}), then run wtx land again"
        ) from exc
    # Before the push: a branch that fails its checks must not reach origin,
    # even force-pushed to its own name.
    if local and not skip_checks:
        _checks(ctx, path)
    # Name the ref. A branch cut with wt tracks its base until setup repairs
    # that, and a bare push then refuses to guess. land must work on a worktree
    # setup never touched. --force-with-lease still guards origin/<branch>.
    run(["git", "push", "--force-with-lease", "origin", branch], cwd=path)

    if local:
        _land_local(ctx, branch, base)
    else:
        _land_pr(
            ctx,
            branch,
            base,
            poll_seconds=poll_seconds,
            timeout_minutes=timeout_minutes,
        )


def _hand_off_cleanup(ctx: Ctx, branch: str, *, keep_branch: bool, merge: bool) -> None:
    """Removing the worktree kills its session, and this land runs in it
    (prefix+X l puts it there). The kill would stop the removal halfway, so
    the tmux server does the clean up, the way `wtx done` hands off.

    --merge goes along: the job is another process, and without it a land
    forced with the flag would end with a push to delete the remote branch.
    """
    argv = [sys.executable, "-m", "wtx", "land", branch, "--cleanup-only"]
    if keep_branch:
        argv.append("--keep-branch")
    if merge:
        argv.append("--merge")
    say("merged. The tmux server removes the worktree, this session is about to close")
    tmux.cmd(["run-shell", "-b", f"cd {shlex.quote(str(ctx.main))} && {shlex.join(argv)}"])


def open_window(session: str, main: Path, branch: str, *, merge: bool = False) -> None:
    """The `land` window, for prefix+X l and prefix+X m.

    It sits in the branch's own session, so a rebase conflict or a failed
    check shows next to the agent that can fix it. It runs from the main
    checkout, the only place land runs. A second press goes back to the
    window: killing it would stop a land halfway.

    The same wtx that runs now, and its PATH, like the review window.
    """
    names = tmux.out(["list-windows", "-t", f"={session}", "-F", "#{window_name}"])
    if WINDOW in names.splitlines():
        tmux.cmd(["select-window", "-t", f"={session}:{WINDOW}"])
        return
    window = [sys.executable, "-m", "wtx", "land", branch, "--window"]
    if merge:
        window.append("--merge")
    tmux.cmd(
        [
            "new-window",
            "-t",
            f"={session}:",
            "-n",
            WINDOW,
            "-c",
            str(main),
            "-e",
            f"PATH={os.environ.get('PATH', '')}",
            shlex.join(window),
        ]
    )


def _refuse_a_branch_cut_from_elsewhere(ctx: Ctx, branch: str, base: str) -> None:
    """A branch cut from a newer protected branch (stage while dev lags) would
    drag every missing commit into its PR. Refuse that, and only that: a base
    that simply moved on since the branch was cut is what the rebase is for.
    """
    for other in ctx.cfg.repo.protected_branches:
        if other == base:
            continue
        ref = f"origin/{other}"
        if not git.branch_exists(ctx.main, ref):
            continue
        ahead = capture(["git", "rev-list", "--count", f"origin/{base}..{ref}"], cwd=ctx.main)
        if ahead.isdigit() and int(ahead) > 0 and git.is_ancestor(ctx.main, ref, branch):
            raise LandError(
                f"{branch} contains all of {ref}, which is {ahead} commit(s) ahead of "
                f"origin/{base}. Landing it would drag those into {base}. "
                f"Fast-forward {base} first (git push origin {other}:{base}), then run again"
            )


def _land_pr(ctx: Ctx, branch: str, base: str, *, poll_seconds: int, timeout_minutes: int) -> None:
    if which("gh") is None:
        raise LandError("gh is not installed, use --local or open the PR by hand")

    state, url = pr_state(ctx.main, branch)
    if not state:
        say(f"opening a pull request into {base}")
        run(
            ["gh", "pr", "create", "--base", base, "--head", branch, "--fill"],
            cwd=ctx.main,
        )
        state, url = pr_state(ctx.main, branch)
    if state == "MERGED":
        say(f"already merged: {url}")
        return
    if url:
        say(f"pull request: {url}")

    code = run(
        ["gh", "pr", "merge", branch, "--merge", "--auto"],
        cwd=ctx.main,
        check=False,
    )
    if code != 0:
        say("auto-merge is not available on this repo, merging now")
        run(["gh", "pr", "merge", branch, "--merge"], cwd=ctx.main)

    if is_dry_run():
        return
    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        state, _ = pr_state(ctx.main, branch)
        if state == "MERGED":
            return
        if state == "CLOSED":
            raise LandError("the pull request was closed without merging")
        time.sleep(poll_seconds)
    raise LandError(
        f"the pull request is still open after {timeout_minutes} minutes. "
        f"It will merge on its own once the checks pass, then run: wtx done {branch}"
    )


def _require_free_base(ctx: Ctx, base: str) -> None:
    """The main checkout must be able to take the base branch and a merge."""
    if not git.is_clean(ctx.main):
        raise LandError(f"the main checkout {ctx.main} has uncommitted changes")
    other = git.worktree_path_for(ctx.main, base)
    if other and other.resolve() != ctx.main.resolve():
        raise LandError(f"{base} is checked out in {other}, cannot merge it here")


def _land_local(ctx: Ctx, branch: str, base: str) -> None:
    _require_free_base(ctx, base)

    run(["git", "checkout", base], cwd=ctx.main)
    run(["git", "pull", "--ff-only"], cwd=ctx.main)
    run(["git", "merge", "--no-ff", branch], cwd=ctx.main)
    run(["git", "push", "origin", base], cwd=ctx.main)


def _land_merge(ctx: Ctx, branch: str, base: str, path: Path, *, skip_checks: bool) -> None:
    """Merge into the local base branch. No fetch, no rebase, no push.

    _refuse_a_branch_cut_from_elsewhere is not called: it only reads origin/*
    refs, and a repo on this road may have none.
    """
    _require_free_base(ctx, base)
    if not skip_checks:
        # In the worktree, like --local: it has its own ports and the main
        # checkout does not. There is no rebase here, so say what is tested.
        behind = capture(["git", "rev-list", "--count", f"{branch}..{base}"], cwd=ctx.main)
        if behind.isdigit() and int(behind) > 0:
            warn(f"{base} moved on, the checks test {branch} alone, not the merge result")
        _checks(ctx, path)

    run(["git", "checkout", base], cwd=ctx.main)
    say(f"merging {branch} into {base}")
    try:
        # --no-edit: the land window is a terminal, git would open an editor.
        run(["git", "merge", "--no-ff", "--no-edit", branch], cwd=ctx.main)
    except CommandError as exc:
        run(["git", "merge", "--abort"], cwd=ctx.main, check=False, quiet=True)
        raise LandError(
            f"could not merge {branch} into {base}, nothing was merged. If it is a "
            f"conflict, fix it in {path} (git merge {base}), commit, then run wtx land again"
        ) from exc
