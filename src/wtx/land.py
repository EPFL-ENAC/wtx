"""Land a branch into the base branch.

This is a human job and it runs from the main checkout only. gh refuses to
delete a branch that is checked out in a worktree, and the whole point of the
push guard is that a worktree never touches the base branch.

prefix+X l runs it from the branch session anyway, in a window whose folder
is the main checkout. See open_window. prefix+X m does the same with --merge.

Some repos must never talk to their remote, or have none. [repo].land = "merge"
or --merge takes the other road: one `git merge --no-ff` in the main checkout,
and nothing here touches the remote. See _land_merge.

land() only merges. finish() shows what landed and cleans up. In the land
window it waits for Enter first: a session that dies under you is how a
landed session went unnoticed. See finish.

The brief and the accepted plans live only in the worktree, so they go away
with it. [repo].plans says where land publishes them first. See _post_plans
and _commit_plans.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import termios
import time
import tty
from dataclasses import dataclass
from pathlib import Path

from . import git, notify, orchestrate, review, tmux, workmux
from .agents.base import PROMPT_SENT
from .config import WtxConfig
from .context import Ctx, session_name
from .proc import CommandError, capture, capture_code, is_dry_run, run, run_shell, say, warn, which

# The window prefix+X l opens in the branch's own session.
WINDOW = "land"


class LandError(Exception):
    pass


@dataclass
class Landed:
    """What a land did. finish() shows it and cleans up after it.

    keep_branch and merge ride along: the clean up can run in another
    process, and an auto-land try reads its flags from its record, not from
    the command line.
    """

    branch: str
    base: str
    merge: bool = False
    keep_branch: bool = False
    # The pull request, on the PR road.
    url: str = ""
    # It was in the base before this land started: nothing was merged now.
    already: bool = False


# The end of every task: the agent must be free to refuse, and never to land.
TASK_END = (
    " If you cannot do it safely, say why in one line and stop. Do not run wtx land: "
    "wtx tries the land again when you stop."
)


class Fixable(LandError):
    """A land stopped on something the branch's agent can fix.

    `wtx land --auto` hands `task` to the agent. A plain land shows the
    message, like any LandError.
    """

    def __init__(self, message: str, task: str) -> None:
        super().__init__(message)
        self.task = f"wtx auto-land: {task}{TASK_END}"


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
            raise Fixable(
                f"check failed: {cmd}",
                f"the check `{cmd}` fails in this worktree, so the branch cannot land. "
                "Run it, fix what it reports, run it again, and commit the fix. If the "
                "fix would change what this branch is for, ask instead.",
            ) from exc


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
            workmux.remove(ctx.main, branch)
        except workmux.WorkmuxError as exc:
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
) -> Landed:
    """Merge the branch into the base. The clean up is finish()'s job."""
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
        raise Fixable(
            f"{path} has uncommitted changes, commit or stash them first",
            f"this worktree has uncommitted changes, and land refuses them. Commit the "
            f"work on {branch}. Leave out what must not be committed (scratch files, "
            "secrets) and say what you left.",
        )
    done = Landed(branch, base, merge=merge, keep_branch=keep_branch)

    if not merge:
        git.fetch(ctx.main)
    if _merge_commit(ctx, done):
        # A PR merged on the forge, a land whose window was closed before
        # Enter: no second rebase, no checks, no second merge. Straight to
        # the recap, where Enter closes the worktree.
        say(f"{branch} already landed in {base}, there is nothing to merge")
        done.already = True
        if not merge and which("gh"):
            done.url = pr_state(ctx.main, branch)[1]
            # A PR merged by hand has no comment yet, and Enter removes the
            # files. After a closed window the marker finds the first one.
            _post_plans(ctx, branch, path)
        return done

    _warn_on_review(ctx, branch, path)  # before the docs commit moves HEAD
    _commit_plans(ctx, branch, path)
    if merge:
        _land_merge(ctx, branch, base, path, skip_checks=skip_checks)
    else:
        done.url = _land_remote(
            ctx,
            branch,
            base,
            path,
            local=local,
            skip_checks=skip_checks,
            poll_seconds=poll_seconds,
            timeout_minutes=timeout_minutes,
        )
    return done


def outcome(ctx: Ctx, branch: str, *, merge: bool) -> str:
    """How a land ended, read from the repo after the clean up.

    Built from facts, not assumed: a clean up can fail halfway (workmux refuses,
    a commit arrives late and the branch is kept), and a "landed" that hides
    a worktree still on disk is how the human ended up running land twice.
    """
    base = ctx.cfg.repo.base_branch
    pushed = ", nothing was pushed" if merge else ""
    if is_dry_run():
        return f"landed {branch} into {base}{pushed}"
    if git.worktree_path_for(ctx.main, branch):
        return f"{branch} is merged into {base}, but its worktree is still there. Run wtx done {branch}"
    kept = git.branch_exists(ctx.main, f"refs/heads/{branch}")
    left = "the branch is kept" if kept else "branch deleted"
    return f"landed {branch} into {base}, worktree removed, {left}{pushed}"


def _merge_commit(ctx: Ctx, landed: Landed) -> str:
    """The merge that brought the branch into the base, or "".

    The PR road reads origin/<base>: the merge is on the forge, and the local
    base can be far behind. The merge road has only the local base.
    """
    head = git.git(["rev-parse", "--verify", "-q", f"refs/heads/{landed.branch}"], cwd=ctx.main)
    if not head:
        return ""
    ref = landed.base if landed.merge else f"origin/{landed.base}"
    return git.merge_commit_of(ctx.main, head, ref)


# More than this and the recap pushes its own first line off the window.
RECAP_COMMITS = 15


def recap(ctx: Ctx, landed: Landed) -> str:
    """What landed, read from the merge commit: the commits and the size.

    Read after the merge and before the clean up, which deletes the branch.
    A merge done by hand as a fast-forward has no merge commit: then only the
    first line.
    """
    branch, base = landed.branch, landed.base
    if landed.already:
        title = f"{branch} already landed in {base}"
    else:
        title = f"landed {branch} into {base}"
    lines = [f"✓ {title}"]
    if landed.url:
        lines.append(f"  pull request {landed.url}")
    if landed.merge:
        lines.append("  merged locally, nothing was pushed")
    merge = "" if is_dry_run() else _merge_commit(ctx, landed)
    if merge:
        log = ["log", "--oneline", "--no-decorate", f"{merge}^1..{merge}^2"]
        commits = git.git(log, cwd=ctx.main).splitlines()
        size = git.git(["diff", "--shortstat", f"{merge}^1", merge], cwd=ctx.main)
        count = f"{len(commits)} commit{'' if len(commits) == 1 else 's'}"
        lines.append(f"  {count}, {size}" if size else f"  {count}")
        lines += [f"    {line}" for line in commits[:RECAP_COMMITS]]
        if len(commits) > RECAP_COMMITS:
            lines.append(f"    ... and {len(commits) - RECAP_COMMITS} more")
    return "\n".join(lines)


def finish(ctx: Ctx, landed: Landed, *, ask: bool = False) -> None:
    """Show what landed, then clean up.

    ask=False is a plain `wtx land` in a terminal: it cleans up at once, the
    way it always did.

    ask=True is the land window. It waits for Enter before the clean up. A
    clean up that ran on its own killed the session under the human, and when
    it did not run (a PR merged later, a window closed early) nothing in the
    session said the branch had landed. Until Enter, the session is marked
    `landed`: in the status line, in `wtx status`, and in a desktop banner, since a PR
    land can take 30 minutes and an auto-land ends while you look elsewhere.
    """
    print(recap(ctx, landed), flush=True)
    branch = landed.branch
    if not ask:
        _clean_up(ctx, landed)
        return
    session = session_name(ctx.repo_name, branch)
    _mark_landed(session, landed)
    what = "the worktree" if landed.keep_branch else "the worktree and the branch"
    print(f"\n  Enter  remove {what}, close this session\n  q      keep them, close this window")
    if is_dry_run():
        say(f"[dry-run] Enter would remove {what} of {branch}")
        return
    if not _wait_for_enter():
        key = "m" if landed.merge else "l"
        say(f"kept. prefix+X {key} shows this again, prefix+X d closes it")
        return
    _clean_up(ctx, landed)


def _clean_up(ctx: Ctx, landed: Landed) -> None:
    branch = landed.branch
    if tmux.current_session() == session_name(ctx.repo_name, branch):
        # The clean up is another process, and it says how the land ended.
        # Here the worktree is still there, so "landed" would be a guess.
        _hand_off_cleanup(ctx, branch, keep_branch=landed.keep_branch, merge=landed.merge)
        return
    cleanup(ctx, branch, keep_branch=landed.keep_branch, remote=not landed.merge)
    say(outcome(ctx, branch, merge=landed.merge))


# The session state of a branch that landed and waits for its clean up.
LANDED = "landed"


def _mark_landed(session: str, landed: Landed) -> None:
    """The mark goes with the session: teardown.close_session settles it."""
    if is_dry_run():
        return
    what = "is in" if landed.already else "landed in"
    state = f"{landed.branch} {what} {landed.base}"
    notify.write_state(session, LANDED, state)
    if tmux.available():
        tmux.set_session_option(session, tmux.STATE_OPTION, LANDED)
    notify.announce("wtx land", f"{state}. Press Enter in its land window to close it")


ENTER = (b"\r", b"\n")
# q, and Esc: the key that means "go away" in every other prompt.
KEEP = (b"q", b"Q", b"\x1b")


def _wait_for_enter() -> bool:
    """True on Enter. False on q, Esc, Ctrl-C or the end of the input.

    One key, no Enter after it: cbreak mode on the land window's terminal.
    Every other key is ignored, so a stray key never removes a worktree. And
    the keys typed before the question are dropped (TCSAFLUSH): an Enter
    pressed during a 30 minute PR wait is not an answer.
    Not a terminal (a test, a pipe): one line, empty for Enter.
    """
    try:
        if not sys.stdin.isatty():
            while True:
                answer = input().strip()
                if not answer:
                    return True
                if answer.encode() in KEEP:
                    return False
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd, termios.TCSAFLUSH)
            while True:
                key = os.read(fd, 1)
                if not key:
                    return False
                if key in ENTER:
                    return True
                if key in KEEP:
                    return False
        finally:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (EOFError, KeyboardInterrupt, OSError, termios.error):
        return False


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
) -> str:
    """The land that goes through origin: rebase, push, then a PR or a merge.

    Returns the PR url, "" with --local. land() fetched just before.
    """
    _refuse_a_branch_cut_from_elsewhere(ctx, branch, base)

    say(f"rebasing {branch} onto origin/{base}")
    try:
        run(["git", "rebase", f"origin/{base}"], cwd=path)
    except CommandError as exc:
        run(["git", "rebase", "--abort"], cwd=path, check=False, quiet=True)
        raise Fixable(
            f"rebase onto origin/{base} has conflicts. Resolve them in {path} "
            f"(git rebase origin/{base}), then run wtx land again",
            f"{branch} does not rebase cleanly onto origin/{base}. In this worktree, "
            f"run `git rebase origin/{base}`, resolve each conflict so both sides keep "
            "what they meant, and finish the rebase. Then run the lint and tests of the "
            "repo. If a conflict is not clear, run `git rebase --abort`.",
        ) from exc
    # Before the push: a branch that fails its checks must not reach origin,
    # even force-pushed to its own name.
    if local and not skip_checks:
        _checks(ctx, path)
    # Name the ref. A new branch tracks its base until setup repairs
    # that, and a bare push then refuses to guess. land must work on a worktree
    # setup never touched. --force-with-lease still guards origin/<branch>.
    run(["git", "push", "--force-with-lease", "origin", branch], cwd=path)

    if local:
        _land_local(ctx, branch, base)
        return ""
    return _land_pr(
        ctx,
        branch,
        base,
        path,
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
    # The job has no client, and the session dies under this one. Name the
    # client now, so the result reaches the screen the human looks at.
    client = tmux.out(["display-message", "-p", "#{client_name}"]).strip()
    if client:
        argv += ["--client", client]
    say("the tmux server removes the worktree, this session is about to close")
    tmux.cmd(["run-shell", "-b", f"cd {shlex.quote(str(ctx.main))} && {shlex.join(argv)}"])


def open_window(
    session: str,
    main: Path,
    branch: str,
    *,
    merge: bool = False,
    auto: bool = False,
    resume: bool = False,
) -> None:
    """The `land` window, for prefix+X l, m and a.

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
    if auto:
        window.append("--auto")
    if resume:
        # From the agent's Stop hook, see autoland.resume.
        window.append("--resume")
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


def _land_pr(
    ctx: Ctx, branch: str, base: str, path: Path, *, poll_seconds: int, timeout_minutes: int
) -> str:
    """Open the PR if needed, merge it, wait until it is merged. Returns its url.

    Once merged, fetch: the merge commit is on the forge, and the recap reads
    it from origin/<base>.
    """
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
    if url:
        say(f"pull request: {url}")
    # Before the merge, and before the merged return: once land goes on, the
    # worktree and its files are removed. A merged PR still takes a comment.
    _post_plans(ctx, branch, path)
    if state == "MERGED":
        say(f"already merged: {url}")
        git.fetch(ctx.main)
        return url

    code = run(
        ["gh", "pr", "merge", branch, "--merge", "--auto"],
        cwd=ctx.main,
        check=False,
    )
    if code != 0:
        say("auto-merge is not available on this repo, merging now")
        run(["gh", "pr", "merge", branch, "--merge"], cwd=ctx.main)

    if is_dry_run():
        return url
    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        state, _ = pr_state(ctx.main, branch)
        if state == "MERGED":
            git.fetch(ctx.main)
            return url
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
        raise Fixable(
            f"could not merge {branch} into {base}, nothing was merged. If it is a "
            f"conflict, fix it in {path} (git merge {base}), commit, then run wtx land again",
            f"{branch} does not merge cleanly into {base}. In this worktree, run "
            f"`git merge {base}`, resolve each conflict so both sides keep what they "
            "meant, and commit the merge. Then run the lint and tests of the repo. If a "
            "conflict is not clear, run `git merge --abort`.",
        ) from exc


# ---------------------------------------------------------------------------
# the brief and the plans, published before the worktree goes


_PLAN_SPLIT = re.compile(rf"(?m)^{re.escape(orchestrate.PLAN_MARK)}\n(?=# Plan, )")


# GitHub refuses a comment over 65,536 characters. The margin covers the
# marker, the opener and the <details> tags around each part.
COMMENT_LIMIT = 60_000


def _plans_parts(path: Path) -> list[tuple[str, str]]:
    """(title, text) for the brief and each accepted plan, empty when the
    worktree has neither.

    wtx's own instruction to the planner is cut from the brief: it is not the
    human's task. Both marks count only as a whole line, so a brief or a plan
    that quotes them in a sentence is not cut there.
    """
    parts: list[tuple[str, str]] = []
    brief_file = path / PROMPT_SENT
    if brief_file.is_file():
        brief = brief_file.read_text(errors="replace")
        brief = brief.rpartition(f"\n{orchestrate.INSTRUCTION_MARK}\n")[0] or brief
        if brief.strip():
            parts.append(("Brief", brief.strip()))
    plan_file = path / orchestrate.PLAN_FILE
    if plan_file.is_file():
        text = plan_file.read_text(errors="replace")
        for entry in _PLAN_SPLIT.split(text)[1:]:
            head, _, body = entry.partition("\n")
            parts.append((head.lstrip("# ").strip(), body.strip()))
    return parts


def _plans_text(path: Path) -> str:
    """The brief and the plans as one markdown file, for plans = "docs"."""
    return "\n\n".join(f"# {t}\n\n{b}" for t, b in _plans_parts(path))


def _cut(text: str, size: int) -> list[str]:
    """Pieces of at most size characters, cut at a line end when one exists."""
    pieces = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > 0 else size
        pieces.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return [*pieces, text]


def _details(title: str, text: str) -> str:
    """One collapsed block. The blank line after </summary> is what makes
    GitHub render the markdown inside. A </details> or an open code fence in
    the text would close the block early or swallow the next ones."""
    text = text.replace("</details>", "&lt;/details&gt;")
    if text.count("```") % 2:
        text += "\n```"
    return f"<details><summary>{title}</summary>\n\n{text}\n\n</details>"


def _comment_texts(parts: list[tuple[str, str]]) -> list[str]:
    """The blocks packed into as few comments as the size limit permits. A
    part too long for one comment is cut at line ends into numbered pieces."""
    blocks = []
    for title, text in parts:
        pieces = _cut(text, COMMENT_LIMIT - 1000)
        for i, piece in enumerate(pieces, 1):
            name = title if len(pieces) == 1 else f"{title}, part {i} of {len(pieces)}"
            blocks.append(_details(name, piece))
    comments: list[list[str]] = [[]]
    for block in blocks:
        if comments[-1] and len("\n\n".join([*comments[-1], block])) > COMMENT_LIMIT:
            comments.append([])
        comments[-1].append(block)
    return ["\n\n".join(c) for c in comments]


def _closing_issues(cwd: Path, branch: str) -> list[str]:
    code, out, _ = _gh(["pr", "view", branch, "--json", "closingIssuesReferences"], cwd)
    if code != 0 or not out:
        return []
    try:
        refs = json.loads(out).get("closingIssuesReferences") or []
    except (json.JSONDecodeError, AttributeError):
        return []
    # The url, not the number: "Fixes other/repo#5" closes an issue that gh
    # would read as #5 of this repo.
    return [
        str(r.get("url") or r["number"]) for r in refs if isinstance(r, dict) and r.get("number")
    ]


def _comments(cwd: Path, kind: str, ref: str) -> str:
    """Every comment on that PR or issue, as one text to look for markers in.

    Anything gh says that is not a list of comments reads as "none yet".
    """
    code, out, _ = _gh([kind, "view", ref, "--json", "comments"], cwd)
    if code != 0 or not out:
        return ""
    try:
        comments = json.loads(out).get("comments") or []
    except (json.JSONDecodeError, AttributeError):
        return ""
    return "\n".join(str(c.get("body", "")) for c in comments if isinstance(c, dict))


def _post_plans(ctx: Ctx, branch: str, path: Path) -> None:
    """The brief and the plans as comments on the PR or its issue.

    Usually one comment. A text over the size limit of GitHub goes in several,
    each with its own marker, so a land that fails halfway posts only the
    missing ones next time. The marker names the branch, so stage 2 of an
    issue (a second PR, a second branch) still posts on that issue. It also
    holds a hash of the text, so a plan accepted after a failed land is posted
    on the next one. A failure only warns: record keeping does not block a
    merge. The body goes through a file, a multi-line argument would break the
    one-line-per-call logs.
    """
    mode = ctx.cfg.repo.plans
    if mode not in ("pr", "issue"):
        return
    parts = _plans_parts(path)
    if not parts:
        return
    targets = [("pr", branch)]
    if mode == "issue":
        issues = _closing_issues(ctx.main, branch)
        if issues:
            targets = [("issue", n) for n in issues]
        else:
            say("the pull request closes no issue, the plans go on the pull request")
    texts = _comment_texts(parts)
    digest = hashlib.sha256("".join(texts).encode()).hexdigest()[:12]
    plans = sum(1 for title, _ in parts if title != "Brief")
    what = "the brief" if plans == 0 else f"{plans} accepted plan" + "s" * (plans > 1)
    if plans and parts[0][0] == "Brief":
        what = f"the brief and {what}"
    for kind, ref in targets:
        posted = _comments(ctx.main, kind, ref)
        for i, text in enumerate(texts, 1):
            part = f" {i}/{len(texts)}" if len(texts) > 1 else ""
            marker = f"<!-- wtx-plans {branch} {digest}{part} -->"
            if marker in posted:
                continue
            # One line people read without a click, the agent text folded below.
            opener = f"wtx posted {what} for `{branch}` when it landed the branch."
            if part:
                opener += f" Part {i} of {len(texts)}."
            say(f"posting the brief and the plans on {kind} {ref}{part}")
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
                fh.write(f"{marker}\n{opener}\n\n{text}\n")
            try:
                code = run(
                    ["gh", kind, "comment", ref, "--body-file", fh.name], cwd=ctx.main, check=False
                )
            finally:
                Path(fh.name).unlink(missing_ok=True)
            if code != 0:
                warn(f"could not comment on {kind} {ref}, the plans were not posted")
                break


def _commit_plans(ctx: Ctx, branch: str, path: Path) -> None:
    """plans = "docs": commit the brief and the plans on the branch itself.

    Runs on every road (pr, --local, --merge), because it is only a commit.
    The same text as the last land means nothing to commit, so a re-run adds
    no commit.
    """
    if ctx.cfg.repo.plans != "docs":
        return
    text = _plans_text(path)
    if not text:
        return
    rel = Path(ctx.cfg.repo.plans_dir) / f"{git.slug(branch)}.md"
    target = path / rel
    # Config validation only warns on this road: refuse here, before a write.
    if not target.resolve().is_relative_to(path.resolve()):
        raise LandError(f"plans_dir {ctx.cfg.repo.plans_dir!r} is outside the repo")
    # HEAD, not the file: a file left by a failed commit must not count as done.
    code, committed, _ = capture_code(["git", "show", f"HEAD:{rel.as_posix()}"], cwd=path)
    if code == 0 and committed == text.strip():
        return
    if is_dry_run():
        say(f"would write and commit {rel}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n")
    say(f"committing the brief and the plans as {rel}")
    try:
        run(["git", "add", "--", str(rel)], cwd=path, quiet=True)
        run(["git", "commit", "-q", "-m", f"docs: plan for {branch}", "--", str(rel)], cwd=path)
    except CommandError as exc:
        # Undo, or the next land stops on a dirty worktree for a file wtx made.
        run(["git", "reset", "-q", "--", str(rel)], cwd=path, check=False, quiet=True)
        target.unlink(missing_ok=True)
        raise LandError(f"could not commit {rel} in {path}") from exc
