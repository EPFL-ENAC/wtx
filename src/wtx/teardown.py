"""Close a worktree's runtime: the session and the ports.

Removing the worktree itself is workmux's job, this is the pre_remove hook that
runs first. A paired worktree in another repo is never removed here: it may
hold work that is not pushed.
"""

from __future__ import annotations

import os

from . import autoland, envfile, notify, orchestrate, ports, repos, review, tmux
from . import pane as pane_mod
from .context import Ctx
from .proc import run_shell, say, warn


def run_teardown(ctx: Ctx) -> None:
    cfg = ctx.cfg
    if cfg.hooks.pre_teardown:
        say(f"hook pre_teardown: {cfg.hooks.pre_teardown}")
        if run_shell(cfg.hooks.pre_teardown, cwd=ctx.root, check=False) != 0:
            warn("pre_teardown hook failed, continuing")

    close_session(ctx.session, kill=not _inside(ctx.session))

    values = envfile.read_worktree(ctx.root)
    if values:
        held = {
            f.env_key: int(values[f.env_key])
            for f in cfg.ports.families
            if values.get(f.env_key, "").isdigit()
        }
        if held:
            ports.free_ports(held)
            say(f"freed ports {', '.join(str(p) for p in held.values())}")

    for note in repos.teardown_notes(ctx):
        say(note)


def _inside(session: str) -> bool:
    """True when this runs in a pane of the session, a human typed `workmux
    remove` there. Killing the session would kill workmux halfway, before it
    removed anything. workmux closes the session itself once it is done.

    A `run-shell` job has $TMUX and no pane: it is never inside.
    """
    pane = os.environ.get("TMUX_PANE", "")
    if not pane or not os.environ.get("TMUX") or not tmux.available():
        return False
    return tmux.out(["display-message", "-p", "-t", pane, "#{session_name}"]) == session


def close_session(session: str, *, kill: bool = True) -> None:
    """The session and what wtx keeps about it, nothing in the checkout.

    `wtx done` also calls it alone when the worktree folder is already gone:
    there is no checkout left to tear down, but the session is still there.
    """
    # Before the session goes: a banner for a worktree that no longer exists
    # sits in the desktop list for ever, and clicking it opens nothing.
    notify.settle(session)
    notify.forget_conversation(session)
    notify.forget_phase(session)
    notify.forget_nudge(session)
    notify.forget_busy(session)
    orchestrate.clear_record(session)
    autoland.clear(session)
    review.clear(session)
    pane_mod.clear_launch(session)

    if kill and tmux.available() and tmux.has_session(session):
        tmux.kill_session(session)
        say(f"killed session {session}")
