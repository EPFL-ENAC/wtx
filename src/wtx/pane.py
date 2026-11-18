"""What a pane runs when workmux opens a session.

workmux builds the panes and types one line in each: `eval "$(wtx pane NAME)"`.
That line asks wtx, at run time, what the pane is:

- workmux marks nothing on a pane, so the pane tags itself with @wt_role and
  its title. agent_pane_id, the handoff and the review paste find it that way.
  Every tmux command here names the pane with -t $TMUX_PANE: without a
  target, tmux takes the active pane of the session, not the one this runs in.
- a pane with `log = true` mirrors itself to .wt-logs/<name>.log before its
  command starts, so no first line is lost. The agent has no tmux socket, the
  file is how it sees a server.
- what it prints is the line to run: cd to the pane's folder, export
  .env.worktree, then the command.

Whether the agent pane starts on its brief, and on which side of the plan and
build handoff, is not in the workmux config: that file is the same for every
worktree. It travels in a one-shot launch record next to the state files,
written by ensure_session just before `workmux open`, read and deleted here.
No record means a plain session.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
from pathlib import Path

from . import envfile, notify, tmux
from .agents import base as agents
from .agents.base import PROMPT_FILE
from .context import Ctx
from .proc import is_dry_run, warn


def launch_file(session: str) -> Path:
    """Beside the state files, but not named *.json: those are states."""
    return notify.state_dir() / f"{notify.safe_name(session)}.launch"


def write_launch(session: str, *, brief: bool, phase: str) -> None:
    """Say how the next agent pane of this session starts.

    Without a brief, any record left over from a failed open goes: a session
    opened later must not fire an old brief.
    """
    if is_dry_run():
        return
    if not brief:
        clear_launch(session)
        return
    with contextlib.suppress(OSError):
        launch_file(session).write_text(json.dumps({"brief": True, "phase": phase}))


def take_launch(session: str) -> dict:
    """Read and delete the record. Only the first agent pane gets the brief:
    the launch line moves PROMPT.md away, a second one would find nothing."""
    path = launch_file(session)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    clear_launch(session)
    return data if isinstance(data, dict) else {}


def clear_launch(session: str) -> None:
    with contextlib.suppress(OSError):
        launch_file(session).unlink(missing_ok=True)


def tag(ctx: Ctx, pane_id: str, name: str, role: str, log: bool) -> None:
    """Mark this pane for wtx, and start its log. Always -t pane_id."""
    tmux.cmd(["select-pane", "-t", pane_id, "-T", name])
    tmux.cmd(["set-option", "-p", "-t", pane_id, tmux.ROLE_OPTION, role])
    tmux.cmd(["set-option", "-w", "-t", pane_id, "pane-border-status", "top"])
    if log:
        tmux.pipe_log(ctx.logs_dir, name, pane_id)


def script(ctx: Ctx, name: str, pane_id: str) -> str:
    """The shell code the pane evals. Empty when there is nothing to run."""
    pane = next((p for p in ctx.cfg.panes.panes if p.name == name), None)
    if pane is None:
        warn(f"no pane named {name} in the wtx.toml of {ctx.main}, plain shell")
        return ""
    if pane_id:
        tag(ctx, pane_id, pane.name, pane.role, pane.log)

    agent = None
    brief = False
    phase = ""
    if pane.role == "agent":
        try:
            agent = agents.get(ctx.agent_tool)
        except KeyError as exc:
            warn(str(exc))
            return ""
        launch = take_launch(ctx.session)
        brief = bool(launch.get("brief")) and (ctx.root / PROMPT_FILE).is_file()
        phase = str(launch.get("phase") or "plan") if brief else ""
    # No [[repos]] here: the launch line never reads them, and resolving them
    # can create a paired worktree, which a pane must not do.
    rctx = tmux.render_ctx(ctx, [], phase=phase)
    line = tmux.pane_command(ctx, pane, agent, rctx, brief=brief)
    load = envfile.load_snippet(ctx.cfg.owned_env_keys)
    folder = shlex.quote(str(ctx.root / pane.cwd))
    return f"cd {folder} && {{ {load}; {line}; }}\n"


def pane_id() -> str:
    return os.environ.get("TMUX_PANE", "")
