"""Git helpers.

The one rule that matters here: the main checkout is always resolved through
`git rev-parse --git-common-dir`. The wt tool exports $WT_MAIN, but it calls
"main" whichever worktree happens to hold the default branch, so $WT_MAIN lies
and is never read anywhere in wtx.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .proc import capture, capture_code, run


def git(args: list[str], *, cwd: Path) -> str:
    return capture(["git", *args], cwd=cwd)


def toplevel(cwd: Path) -> Path | None:
    out = capture(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    return Path(out) if out else None


def common_dir(cwd: Path) -> Path | None:
    out = capture(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd)
    return Path(out) if out else None


def main_checkout(cwd: Path) -> Path | None:
    """The directory holding the real .git, whatever worktree we stand in."""
    cd = common_dir(cwd)
    return cd.parent if cd else None


def in_worktree(cwd: Path) -> bool:
    top = toplevel(cwd)
    main = main_checkout(cwd)
    return bool(top and main and top.resolve() != main.resolve())


def current_branch(cwd: Path) -> str:
    return capture(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)


def repo_name(cwd: Path) -> str:
    url = capture(["git", "config", "--get", "remote.origin.url"], cwd=cwd)
    if url:
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            return name
    main = main_checkout(cwd)
    return main.name if main else cwd.name


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    branch: str
    head: str


def list_worktrees(cwd: Path) -> list[WorktreeInfo]:
    out = capture(["git", "worktree", "list", "--porcelain"], cwd=cwd)
    items: list[WorktreeInfo] = []
    path = head = branch = ""
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("HEAD "):
            head = line[len("HEAD ") :]
        elif line.startswith("branch "):
            branch = line[len("branch ") :].removeprefix("refs/heads/")
        elif not line.strip():
            if path:
                items.append(WorktreeInfo(Path(path), branch, head))
            path = head = branch = ""
    return items


def worktree_path_for(cwd: Path, branch: str) -> Path | None:
    """Where `branch` is checked out, or None.

    Prunes first and checks the .git entry really exists. A half deleted
    worktree stays in `git worktree list` and once resolved to the main
    checkout, which made a guard exempt itself and let a push through.
    """
    run(["git", "worktree", "prune"], cwd=cwd, check=False, quiet=True, mutating=False)
    for wt in list_worktrees(cwd):
        if wt.branch == branch:
            if (wt.path / ".git").exists():
                return wt.path
            return None
    return None


def branch_exists(cwd: Path, ref: str) -> bool:
    code, _, _ = capture_code(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=cwd
    )
    return code == 0


def is_clean(cwd: Path) -> bool:
    return capture(["git", "status", "--porcelain"], cwd=cwd) == ""


def is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    code, _, _ = capture_code(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=cwd)
    return code == 0


def slug(branch: str) -> str:
    """Worktree directory name for a branch, matching wt's separator."""
    return branch.replace("/", "-")


def repair_tracking(cwd: Path, branch: str, base_branch: str) -> None:
    """Stop a new branch from tracking the branch it was cut from.

    `wt create x origin/dev` leaves x tracking origin/dev with the default
    autoSetupMerge. Then `git pull` rebases the work onto dev and the next push
    is refused. Setting these two repo-level keys and clearing a wrong upstream
    is the fix, and it runs on every setup because a branch can predate it.
    """
    run(
        ["git", "config", "branch.autoSetupMerge", "simple"],
        cwd=cwd,
        check=False,
        quiet=True,
    )
    run(
        ["git", "config", "push.autoSetupRemote", "true"],
        cwd=cwd,
        check=False,
        quiet=True,
    )
    upstream = capture(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=cwd,
    )
    if upstream and upstream != f"origin/{branch}":
        wrong_base = upstream in (f"origin/{base_branch}", base_branch)
        if wrong_base:
            run(
                ["git", "branch", "--unset-upstream"],
                cwd=cwd,
                check=False,
                quiet=True,
            )


def fetch(cwd: Path, remote: str = "origin") -> None:
    run(["git", "fetch", remote], cwd=cwd, check=False, mutating=False)
