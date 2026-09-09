"""Wrapper around the wt binary (github.com/timvw/wt).

Two rules:

- Always pass --format json. In text mode wt prints an auto-cd marker on stdout
  that a shell wrapper follows, so a nested call made while creating a worktree
  would move the user's shell into the other repo.
- Never trust wt for the resulting path. Ask git afterwards. wt's own idea of
  "main" is whichever worktree holds the default branch, which is not what we
  mean by the main checkout.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import git
from .proc import capture_code, is_dry_run, run, warn


class WtError(Exception):
    pass


def available() -> bool:
    from .proc import which

    return which("wt") is not None


def version() -> str:
    code, out, _ = capture_code(["wt", "version"])
    return out if code == 0 else ""


def _run_json(args: list[str], cwd: Path) -> dict:
    """Run wt and return its parsed payload. stdout never reaches our caller."""
    if is_dry_run():
        run(["wt", "--format", "json", *args], cwd=cwd, mutating=True)
        return {}
    code, out, err = capture_code(["wt", "--format", "json", *args], cwd=cwd)
    payload: dict = {}
    if out:
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            payload = {}
    if code != 0:
        message = payload.get("error") or err or out or f"wt {' '.join(args)} failed"
        raise WtError(str(message))
    return payload


def create(cwd: Path, branch: str, base: str) -> Path | None:
    """New branch in a new worktree. Fires that repo's own .wt.toml hooks."""
    _run_json(["create", branch, base], cwd)
    return git.worktree_path_for(cwd, branch)


def checkout(cwd: Path, branch: str) -> Path | None:
    """Existing branch in a new worktree."""
    _run_json(["checkout", branch], cwd)
    return git.worktree_path_for(cwd, branch)


def ensure(cwd: Path, branch: str, base: str) -> tuple[Path | None, bool]:
    """Worktree for branch, creating or checking out as needed.

    Returns (path, existed_before).
    """
    existing = git.worktree_path_for(cwd, branch)
    if existing:
        return existing, True
    if git.branch_exists(cwd, branch) or git.branch_exists(cwd, f"origin/{branch}"):
        return checkout(cwd, branch), False
    return create(cwd, branch, base), False


def remove(cwd: Path, branch: str, *, force: bool = False) -> None:
    """Remove a worktree. Fires that repo's pre_remove hook."""
    args = ["remove", branch]
    if force:
        args.append("--force")
    try:
        _run_json(args, cwd)
    except WtError as exc:
        warn(f"wt remove {branch}: {exc}")
        raise
