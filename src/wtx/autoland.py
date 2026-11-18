"""Auto-land: land, and when the land stops on something the agent can fix,
give it to the agent and try again when it stops.

How it runs:

1. A human asks for it: `prefix+X a`, or `wtx land <branch> --auto`. Never
   the agent: the record lives in the runtime dir, which the sandbox cannot
   write, and the push guard still stands.
2. If the agent is in the middle of a turn, the land waits: a rebase under
   a working agent breaks its work. A record is written and nothing else.
3. Else the land runs, in the `land` window of the branch session. When it
   stops on a `land.Fixable` (uncommitted work, a conflict, a failed check),
   the agent pane is restarted on its own conversation with the task as the
   next message, like the plan handoff in orchestrate.py. The record keeps
   the land flags, the number of tries and the HEAD after the last one.
4. When the agent stops, its Stop hook (notify.handle) finds the record and
   opens the land window again. Steps 3 and 4 repeat.

It gives up, and says why in the window and on the desktop, after MAX_TRIES
tasks, when HEAD did not move since the last try (the agent changed nothing,
or said it could not), or when the agent cannot be resumed. Any other land
error stops it too: those are for the human.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from . import config as config_mod
from . import context, git, notify, repos, tmux
from .agents import base as agents
from .context import Ctx, session_name
from .proc import is_dry_run, say

if TYPE_CHECKING:
    from .land import Landed

MAX_TRIES = 3

# The land flags a record keeps, so every try lands the same way.
FLAGS = ("merge", "local", "skip_checks", "keep_branch")


class GiveUp(Exception):
    """Auto-land stops here. The human takes over."""


def record_file(session: str) -> Path:
    """Beside the state files, but not named *.json: those are states."""
    return notify.state_dir() / f"{notify.safe_name(session)}.autoland"


def read(session: str) -> dict:
    try:
        data = json.loads(record_file(session).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write(session: str, data: dict) -> None:
    if is_dry_run():
        return
    with contextlib.suppress(OSError):
        record_file(session).write_text(json.dumps(data))


def clear(session: str) -> None:
    with contextlib.suppress(OSError):
        record_file(session).unlink(missing_ok=True)


def resume(session: str) -> bool:
    """The Stop hook: open the land window again if an auto-land waits.

    The hook runs in the agent pane, and the try may restart that pane. So the
    try runs in the window, a process of its own, a little later.
    """
    data = read(session)
    if not data.get("branch") or not data.get("main"):
        return False
    if not tmux.available() or not tmux.has_session(session):
        return False
    from . import land as land_mod

    land_mod.open_window(
        session,
        Path(data["main"]),
        data["branch"],
        merge=bool(data.get("merge")),
        auto=True,
        resume=True,
    )
    return True


def _head(path: Path | None) -> str:
    if path is None:
        return ""
    return git.git(["rev-parse", "HEAD"], cwd=path)


def run(ctx: Ctx, branch: str, flags: dict, *, resume: bool = False) -> str | Landed:
    """One try, from the main checkout. Returns what landed, or what to tell
    the human when it did not land. land.finish shows the first.

    Raises GiveUp when the human must take over, and LandError for a stop the
    agent cannot fix. Both end the auto-land. `resume` is a try the Stop hook
    asked for: with no record left, someone turned the auto-land off.
    """
    from . import land as land_mod

    session = session_name(ctx.repo_name, branch)
    data = read(session)
    if resume and not data:
        return f"auto-land of {branch} is off, nothing to do"
    # The record keeps the flags of the first try, so every try lands the same.
    flags = {k: bool(data.get(k, flags.get(k))) for k in FLAGS}
    tries = int(data.get("tries", 0))
    path = git.worktree_path_for(ctx.main, branch)

    if notify.is_busy(session):
        write(session, _record(ctx, branch, flags, tries, data.get("head", "")))
        return (
            f"auto-land of {branch} waits for the agent to stop. "
            f"To cancel: wtx land {branch} --stop-auto"
        )
    if tries and data.get("head") and data["head"] == _head(path):
        clear(session)
        raise GiveUp(f"the agent changed nothing on {branch} since the last try")

    # Taken now: a land that works ends the auto-land, and so does a stop
    # that is not ours to fix.
    clear(session)
    try:
        landed = land_mod.land(ctx, branch, **flags)
    except land_mod.Fixable as exc:
        tries += 1
        if tries > MAX_TRIES:
            raise GiveUp(f"{MAX_TRIES} tries used, and the land still stops: {exc}") from exc
        _hand_to_agent(ctx, branch, exc.task)
        write(session, _record(ctx, branch, flags, tries, _head(path)))
        return f"auto-land try {tries} of {MAX_TRIES}: {exc}. The agent is on it"
    return landed


def _record(ctx: Ctx, branch: str, flags: dict, tries: int, head: str) -> dict:
    return {"branch": branch, "main": str(ctx.main), **flags, "tries": tries, "head": head}


def _hand_to_agent(ctx: Ctx, branch: str, task: str) -> None:
    """Restart the agent pane on its own conversation, with the task as its
    next message. The road of the plan handoff: see orchestrate.run."""
    session = session_name(ctx.repo_name, branch)
    path = git.worktree_path_for(ctx.main, branch)
    if path is None:
        raise GiveUp(f"no worktree for {branch}")
    try:
        wctx = context.load(root=path)
    except (context.ContextError, config_mod.ConfigError) as exc:
        raise GiveUp(f"cannot read the worktree of {branch}: {exc}") from exc
    agent = agents.get(wctx.agent_tool)
    conversation = notify.conversation(session)
    if not conversation:
        raise GiveUp(
            "no conversation to give the task to. The agent hooks tell wtx its id: "
            "run wtx install-machine --apply"
        )
    rctx = tmux.render_ctx(wctx, repos.resolve_all(wctx), phase="build")
    line = agent.handoff_cmd(rctx, session=conversation, prompt=task)
    if not line:
        raise GiveUp(f"{agent.name} cannot take a task from wtx")
    notify.remember_phase(session, "build")
    if not tmux.respawn_agent(wctx, line, note=f"auto-land: sending the agent of {branch} a task"):
        raise GiveUp(f"no agent pane in {session}")


def stop(ctx: Ctx, branch: str) -> None:
    session = session_name(ctx.repo_name, branch)
    if not read(session):
        say(f"no auto-land waits for {branch}")
        return
    clear(session)
    say(f"auto-land of {branch} is off")


def announce(branch: str, why: str) -> None:
    """The try may run while nobody looks at the session."""
    notify.announce("wtx auto-land", f"{branch} needs you: {why}")
