"""The pre-push guard.

Inside a worktree a push may only update that worktree's own branch, and never
a protected one. The main checkout is free: that is where `wtx land` lands
branches.

It is a plain .git/hooks/pre-push, not a lefthook job, on purpose. lefthook
decides a pre-push job "has no matching files" and skips it, which it did on a
real push to a protected branch. git runs .git/hooks/pre-push every time, with
the refs on stdin.

The branch list and the whole guard body are baked into the hook. It must not
depend on a file that an old branch, or a branch someone reset, might not have.

A hook manager runs every pre-push.* it finds (lefthook runs pre-push.old). A
copy of the guard under another name then calls the chain, which calls the copy
again, forever. So the guard stops on re-entry, and install removes the copies.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from .config import WtxConfig
from .proc import capture, say, warn, would_write

MARKER = "# wtx push guard"
PREVIOUS = "pre-push.before-wt"
# Set by the guard while it runs. A second guard in the same push sees it and
# exits: a loop through lefthook once left 33,000 processes.
REENTRY_VAR = "WTX_PUSH_GUARD_RUNNING"


def hooks_dir(main: Path) -> Path:
    """Where git looks for hooks. core.hooksPath wins over .git/hooks.

    A guard installed in the directory git ignores would never run. Worktrees
    share this directory with the main checkout, so one install covers all of
    them.
    """
    configured = capture(["git", "config", "--get", "core.hooksPath"], cwd=main)
    if configured:
        p = Path(configured)
        return p if p.is_absolute() else (main / configured)
    common = capture(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=main)
    return Path(common) / "hooks"


def render_hook(cfg: WtxConfig) -> str:
    protected = " ".join(cfg.repo.protected_branches)
    return f"""#!/bin/sh
{MARKER}, installed by wtx. Shared by every checkout of this repo.
# A worktree may push only its own branch, never a protected one. The main
# checkout is unrestricted, that is where `wtx land` runs.
# The agent's own deny rules cover the same pushes, but this runs inside git,
# so it holds for any process in the worktree and cannot be talked around.
set -u
# lefthook runs every pre-push.* it finds, a stale copy of this guard included.
# Once is enough: a second entry exits before it can loop.
if [ -n "${{{REENTRY_VAR}:-}}" ]; then
  exit 0
fi
{REENTRY_VAR}=1
export {REENTRY_VAR}
refs=$(cat)

wtx_guard() {{
  if [ "$(git rev-parse --git-dir)" = "$(git rev-parse --git-common-dir)" ]; then
    return 0
  fi
  protected={shlex.quote(protected)}
  branch=$(git rev-parse --abbrev-ref HEAD)
  refuse() {{
    echo "wtx push-guard: $*" >&2
    echo "wtx push-guard: land it from the main checkout with: wtx land $branch" >&2
    exit 1
  }}
  saw_ref=0
  while read -r _local_ref _local_sha remote_ref _remote_sha; do
    [ -n "${{remote_ref:-}}" ] || continue
    saw_ref=1
    target="${{remote_ref#refs/heads/}}"
    for p in $protected; do
      [ "$target" != "$p" ] || refuse "a worktree never pushes protected branch '$p'"
    done
    [ "$target" = "$branch" ] || \\
      refuse "worktree on '$branch' may only push '$branch', not '$target'"
  done
  # No ref lines on stdin (an unexpected hook runner): still keep the protected
  # branches safe.
  if [ "$saw_ref" = 0 ]; then
    for p in $protected; do
      [ "$branch" != "$p" ] || refuse "a worktree never pushes protected branch '$p'"
    done
  fi
  return 0
}}

printf '%s\\n' "$refs" | wtx_guard || exit 1

# A pre-push hook that was already here still runs, with the same refs.
prev="$(dirname "$0")/{PREVIOUS}"
[ -x "$prev" ] || exit 0
printf '%s\\n' "$refs" | "$prev" "$@"
"""


def is_installed(main: Path) -> bool:
    hook = hooks_dir(main) / "pre-push"
    return hook.is_file() and MARKER in hook.read_text()


def stale_copies(directory: Path) -> list[Path]:
    """Every pre-push.* in the hooks dir that holds the guard.

    `lefthook install` moves the guard to pre-push.old and lefthook runs it
    from there, so the guard runs twice and, through the chain, loops.
    """
    if not directory.is_dir():
        return []
    found = []
    for p in sorted(directory.glob("pre-push.*")):
        try:
            if p.is_file() and MARKER in p.read_text(errors="replace"):
                found.append(p)
        except OSError:
            continue
    return found


def install(cfg: WtxConfig, main: Path) -> bool:
    """Write the guard, chaining any hook that was already there.

    Runs after the dependency install so a `lefthook install` is not the one
    that replaces it.
    """
    directory = hooks_dir(main)
    hook = directory / "pre-push"
    if would_write(hook):
        return True
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for stale in stale_copies(directory):
            stale.unlink()
            say(f"removed {stale.name}, a stale copy of the push guard (a hook manager runs it)")
        # Only a foreign hook is chained. The guard as PREVIOUS would run itself.
        if hook.is_file() and MARKER not in hook.read_text():
            hook.replace(directory / PREVIOUS)
            say(f"kept the existing pre-push hook as {PREVIOUS}, it still runs")
        hook.write_text(render_hook(cfg))
        hook.chmod(0o755)
    except OSError as exc:
        # A sandboxed agent cannot write .git/hooks. Worktree creation is a
        # terminal job; say so rather than leaving a repo unguarded quietly.
        warn(f"could not install the push guard in {directory}: {exc}")
        warn("run `wtx setup` from a real terminal, this worktree is unguarded")
        return False
    return True
