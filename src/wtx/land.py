"""Land a branch into the base branch.

This is a human job and it runs from the main checkout only. gh refuses to
delete a branch that is checked out in a worktree, and the whole point of the
push guard is that a worktree never touches the base branch.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import git, wt
from .context import Ctx
from .proc import CommandError, capture, capture_code, run, run_shell, say, warn


class LandError(Exception):
    pass


def _gh(args: list[str], cwd: Path) -> tuple[int, str, str]:
    return capture_code(["gh", *args], cwd=cwd)


def pr_state(cwd: Path, branch: str) -> tuple[str, str]:
    """(state, url) of the branch's pull request, empty when there is none."""
    code, out, _ = _gh(
        ["pr", "view", branch, "--json", "state,url"], cwd
    )
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


def cleanup(ctx: Ctx, branch: str, *, keep_branch: bool = False) -> None:
    """Remove the worktree and the branch, local and remote."""
    if git.worktree_path_for(ctx.main, branch):
        try:
            wt.remove(ctx.main, branch)
        except wt.WtError as exc:
            warn(f"could not remove the worktree: {exc}")
    if keep_branch:
        return
    run(["git", "branch", "-D", branch], cwd=ctx.main, check=False, quiet=True)
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
    skip_checks: bool = False,
    keep_branch: bool = False,
    poll_seconds: int = 20,
    timeout_minutes: int = 30,
) -> None:
    cfg = ctx.cfg
    base = cfg.repo.base_branch

    if ctx.is_worktree:
        raise LandError(
            f"run this from the main checkout ({ctx.main}), not from a worktree"
        )
    if branch in cfg.repo.protected_branches:
        raise LandError(f"{branch} is a protected branch, there is nothing to land")

    path = git.worktree_path_for(ctx.main, branch)
    if path is None:
        raise LandError(f"no worktree for {branch}")
    if not git.is_clean(path):
        raise LandError(f"{path} has uncommitted changes, commit or stash them first")

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
    run(["git", "push", "--force-with-lease"], cwd=path)

    if local:
        _land_local(ctx, branch, path, base, skip_checks=skip_checks)
    else:
        _land_pr(
            ctx,
            branch,
            base,
            poll_seconds=poll_seconds,
            timeout_minutes=timeout_minutes,
        )
    cleanup(ctx, branch, keep_branch=keep_branch)
    say(f"landed {branch} into {base}")


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


def _land_pr(
    ctx: Ctx, branch: str, base: str, *, poll_seconds: int, timeout_minutes: int
) -> None:
    from .proc import is_dry_run, which

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
        ["gh", "pr", "merge", branch, "--squash", "--auto"],
        cwd=ctx.main,
        check=False,
    )
    if code != 0:
        say("auto-merge is not available on this repo, merging now")
        run(["gh", "pr", "merge", branch, "--squash"], cwd=ctx.main)

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


def _land_local(ctx: Ctx, branch: str, path: Path, base: str, *, skip_checks: bool) -> None:
    if not git.is_clean(ctx.main):
        raise LandError(f"the main checkout {ctx.main} has uncommitted changes")
    other = git.worktree_path_for(ctx.main, base)
    if other and other.resolve() != ctx.main.resolve():
        raise LandError(f"{base} is checked out in {other}, cannot merge it here")

    if not skip_checks:
        _checks(ctx, path)

    run(["git", "checkout", base], cwd=ctx.main)
    run(["git", "pull", "--ff-only"], cwd=ctx.main)
    run(["git", "merge", "--no-ff", branch], cwd=ctx.main)
    run(["git", "push", "origin", base], cwd=ctx.main)


def status_line(ctx: Ctx, branch: str) -> str:
    state, url = pr_state(ctx.main, branch)
    if not state:
        return "no pull request"
    return f"{state.lower()} {url}".strip()


def head_of(cwd: Path, ref: str) -> str:
    return capture(["git", "rev-parse", "--short", ref], cwd=cwd)
