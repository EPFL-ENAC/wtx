"""External repos an agent needs: data repos, sibling libraries, the k8s config.

Two access modes.

read
    The agent may read the directory with no prompt and may never write in it.
    The k8s GitOps config in its shared main checkout is the plain case: everyone
    reads it, nobody edits it in place.

pair
    Same reading, plus, when the user asked for it with `--with name[=branch]`,
    its own worktree and branch in that repo. The agent edits there, and the
    change lands through that repo's own pull request. Without --with, a pair
    repo behaves exactly like a read repo.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import git, wt
from .config import ExtRepo
from .context import Ctx
from .proc import say, warn

WITH_ENV = "WTX_WITH"


@dataclass
class Resolved:
    """An external repo as this worktree sees it."""

    spec: ExtRepo
    path: Path  # what the agent gets access to
    branch: str  # the paired branch, empty when not paired
    exists: bool

    @property
    def paired(self) -> bool:
        return bool(self.branch)

    @property
    def mode(self) -> str:
        return "pair" if self.paired else "read"


def parse_with(values: list[str]) -> dict[str, str]:
    """Turn --with name / --with name=branch into {name: branch}.

    An empty branch means "pair it on a branch named like mine": resolve_all
    fills in the app branch. Reading needs no --with at all.
    """
    out: dict[str, str] = {}
    for raw in values:
        name, sep, branch = raw.partition("=")
        name = name.strip()
        if not name:
            continue
        out[name] = branch.strip() if sep else ""
    return out


def encode_with(pairs: dict[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in pairs.items())


def decode_with(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    return parse_with([part for part in raw.split(",") if part])


def _pair_branch_from_env(env: dict[str, str], spec: ExtRepo) -> str:
    return env.get(spec.branch_key, "")


def resolve_all(ctx: Ctx, *, requested: dict[str, str] | None = None) -> list[Resolved]:
    """Work out, for every [[repos]] entry, which directory this worktree gets.

    Pairings are sticky: a branch remembered in .env.worktree is re-paired on
    every setup, so a plain `wtx go` never silently un-pairs a worktree.
    """
    requested = requested or {}
    unknown = set(requested) - {r.name for r in ctx.cfg.repos}
    for name in sorted(unknown):
        warn(f"--with {name}: no [[repos]] entry named {name} in wtx.toml, ignored")

    out: list[Resolved] = []
    for spec in ctx.cfg.repos:
        main_path = spec.resolve(ctx.main)
        if not main_path.exists():
            warn(f"[[repos]] {spec.name}: {main_path} does not exist, skipped")
            out.append(Resolved(spec, main_path, "", exists=False))
            continue

        want = requested.get(spec.name, _pair_branch_from_env(ctx.env, spec))
        if spec.name in requested and not want and spec.access == "pair":
            want = ctx.branch
        if not want or spec.access != "pair":
            if want and spec.access != "pair":
                warn(f'[[repos]] {spec.name} is access = "read", --with ignored')
            out.append(Resolved(spec, main_path, "", exists=True))
            continue

        path = _ensure_pair(main_path, spec, want)
        if path is None:
            out.append(Resolved(spec, main_path, "", exists=True))
            continue
        out.append(Resolved(spec, path, want, exists=True))
    return out


def _ensure_pair(main_path: Path, spec: ExtRepo, branch: str) -> Path | None:
    """Worktree for `branch` inside the external repo, created if needed.

    The nested wt call runs inside that repo, so that repo's own .wt.toml hooks
    fire and it gets its own session. It goes through wt.py, which keeps wt's
    auto-cd marker away from the caller's shell.
    """
    if git.main_checkout(main_path) is None:
        warn(f"[[repos]] {spec.name}: {main_path} is not a git repo, cannot pair")
        return None
    existing = git.worktree_path_for(main_path, branch)
    if existing:
        return existing
    if not wt.available():
        warn(f"[[repos]] {spec.name}: wt is not installed, cannot pair {branch}")
        return None
    git.fetch(main_path)
    try:
        path, _ = wt.ensure(main_path, branch, f"origin/{spec.base_branch}")
    except wt.WtError as exc:
        warn(f"[[repos]] {spec.name}: could not pair {branch}: {exc}")
        return None
    if path:
        say(f"paired {spec.name} on {branch} at {path}")
    return path


def env_values(resolved: list[Resolved]) -> dict[str, str]:
    """The .env.worktree keys these repos contribute."""
    out: dict[str, str] = {}
    for r in resolved:
        if not r.exists:
            continue
        out[r.spec.path_key] = str(r.path)
        out[r.spec.branch_key] = r.branch
    return out


def teardown_notes(ctx: Ctx) -> list[str]:
    """What to tell the user when closing a worktree that paired something.

    A paired worktree may hold work that is not pushed, so wtx never removes it
    along with the app worktree.
    """
    notes: list[str] = []
    for spec in ctx.cfg.repos:
        branch = ctx.env.get(spec.branch_key, "")
        path = ctx.env.get(spec.path_key, "")
        if branch and path:
            notes.append(
                f"{spec.name} still has a worktree on {branch}. "
                f"Close it with: cd {Path(path).parent.parent.parent} && wtx done {branch}"
            )
    return notes
