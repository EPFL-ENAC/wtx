"""Close a worktree's runtime: the session and the ports.

Removing the worktree itself is wt's job, this is the pre_remove hook that runs
first. A paired worktree in another repo is never removed here: it may hold work
that is not pushed.
"""

from __future__ import annotations

from . import envfile, ports, repos, tmux
from .context import Ctx
from .proc import run_shell, say, warn


def run_teardown(ctx: Ctx) -> None:
    cfg = ctx.cfg
    if cfg.hooks.pre_teardown:
        say(f"hook pre_teardown: {cfg.hooks.pre_teardown}")
        if run_shell(cfg.hooks.pre_teardown, cwd=ctx.root, check=False) != 0:
            warn("pre_teardown hook failed, continuing")

    if tmux.available() and tmux.has_session(ctx.session):
        tmux.kill_session(ctx.session)
        say(f"killed session {ctx.session}")

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
