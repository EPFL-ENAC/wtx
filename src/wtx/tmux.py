"""The tmux session for a checkout, and what wtx still asks tmux itself.

workmux builds the session: one per checkout, named "<repo>/<handle>", one
window, the agent pane on the left and the rest stacked on the right (see
workmux.py). What workmux has no command for stays here: restarting the agent
pane, the server-wide key bindings, the session state option, attach.

Panes are addressed through the @wt_role option, which each pane sets on
itself (see pane.py), never by title: a coding agent rewrites its own pane
title as it works, and a title is not a tmux target anyway.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Sequence
from pathlib import Path

from . import envfile
from .agents.base import PROMPT_FILE, Agent, RenderContext
from .config import Pane
from .context import Ctx
from .proc import capture, capture_code, is_dry_run, run, say, warn

STATE_OPTION = "@wtx_state"
ROLE_OPTION = "@wt_role"
WINDOW = "dev"
# The session wtx starts when there is no tmux server: workmux refuses to run
# without one. It stays, it is what keeps the server up.
HOME_SESSION = "wtx-home"


def available() -> bool:
    from .proc import which

    return which("tmux") is not None and server_reachable()


def cmd(args: list[str], *, check: bool = False) -> int:
    return run(["tmux", *args], check=check, quiet=True)


def out(args: list[str], *, mutating: bool = False) -> str:
    """What tmux printed. Reading is always safe, so only a query that also
    changes something (a split that prints the new pane id) honours dry run."""
    if mutating and is_dry_run():
        print(f"would run: {shlex.join(['tmux', *args])}")
        return ""
    return capture(["tmux", *args])


def server_status() -> tuple[str, str]:
    """How the tmux server looks from here, and what tmux said about it.

    When the socket cannot be used, which is what an agent sandbox does, tmux
    still exits 0 and only complains on stderr. Believing the exit code makes
    every session look present, so wtx never creates one and quietly attaches to
    nothing.

    The states:

    - "up", a server answers
    - "absent", no server yet, and new-session starts one. tmux says it two
      ways: "no server running" when a socket is left with nothing behind it
      (tmux removes it itself), and "error connecting ... (No such file or
      directory)" when there is no socket at all, the usual case after a reboot
    - "blocked", the socket cannot be used, which is the sandbox case
    - "unknown", anything else, passed on as tmux worded it
    """
    code, _, err = capture_code(["tmux", "list-sessions"])
    if not err:
        return ("up" if code == 0 else "absent"), ""
    low = err.lower()
    if "no server running" in low or "no such file or directory" in low:
        return "absent", err
    if "operation not permitted" in low or "permission denied" in low:
        return "blocked", err
    return "unknown", err


def server_reachable() -> bool:
    """True when wtx can use a server, or start one."""
    return server_status()[0] in ("up", "absent")


def server_remedy(state: str, err: str) -> str:
    """What to do about a server wtx cannot use."""
    if state == "blocked":
        return "run wtx from a real terminal, an agent sandbox blocks the tmux socket"
    return f"tmux said: {err}"


def has_session(name: str) -> bool:
    code, _, err = capture_code(["tmux", "has-session", "-t", f"={name}"])
    return code == 0 and not err


def current_session() -> str:
    """The session of the pane this runs in, empty outside tmux.

    tmux finds the pane from $TMUX_PANE. A run-shell job has $TMUX and no
    pane, so tmux guesses: a job must not ask.
    """
    if not os.environ.get("TMUX") or not available():
        return ""
    return out(["display-message", "-p", "#{session_name}"])


def list_sessions() -> list[str]:
    names = out(["list-sessions", "-F", "#{session_name}"])
    return [line for line in names.splitlines() if line]


def set_session_option(session: str, key: str, value: str) -> None:
    cmd(["set-option", "-t", f"={session}:", key, value])


def kill_session(session: str) -> None:
    if has_session(session):
        cmd(["kill-session", "-t", f"={session}"])


def ensure_server() -> None:
    """Start a tmux server when there is none. workmux add and open refuse to
    run without one, and wtx go or wtx all after a reboot must still work from
    a plain terminal. A session is the only way to keep a server up."""
    if server_status()[0] != "absent":
        return
    say(f"no tmux server, starting one with a session {HOME_SESSION}")
    cmd(["new-session", "-d", "-s", HOME_SESSION, "-c", str(Path.home())])


def scrub_environment(keys: Sequence[str]) -> None:
    """Drop wtx values from the tmux server environment.

    If the command that starts the tmux server was run from a shell with a
    worktree's ports exported, every later pane in every session inherits them,
    including the main checkout's, whose frontend then looks for a backend that
    is not there.
    """
    from .hooks import GO_AGENT_ENV, GO_DRIVING_ENV, GO_LLM_ENV
    from .repos import WITH_ENV

    for key in ("ROOT", WITH_ENV, GO_AGENT_ENV, GO_LLM_ENV, GO_DRIVING_ENV, *keys):
        cmd(["set-environment", "-gu", key])


def server_options() -> None:
    """Server-wide settings, refreshed on every run.

    detach-on-destroy off: killing a session moves the client to the next one
    instead of dropping out of tmux.

    prefix+X: a small menu on the current session, with review, done, land,
    merge and auto land.
    prefix+r: the review of the branch, in hunk. The bindings are server wide
    and the last run owns them, whatever repo it came from, so they bake in no
    path: wtx is on PATH and the branch comes from the pane's own directory.
    tmux parses the snippet before sh does, so it uses no dollar signs and no
    double quotes.
    """
    cmd(["set-option", "-g", "detach-on-destroy", "off"])
    resolve = "wtx done --path '#{pane_current_path}' --session '#{session_name}' --from-tmux"
    close = f'run-shell -b "{resolve}"'
    discard = (
        "confirm-before -p 'discard uncommitted changes and remove the worktree? (y/n)' "
        f'"run-shell -b \\"{resolve} --force\\""'
    )
    land = "wtx land --path '#{pane_current_path}' --session '#{session_name}' --from-tmux"
    review = "wtx review --path '#{pane_current_path}' --from-tmux"
    cmd(
        [
            "bind-key",
            "X",
            "display-menu",
            "-T",
            " #S ",
            "review: open the branch review in hunk",
            "r",
            f'run-shell -b "{review}"',
            "",
            "kill session (stay in tmux)",
            "k",
            "kill-session",
            "",
            "done: also remove worktree, keep branch",
            "d",
            close,
            "force done: DISCARD uncommitted changes",
            "D",
            discard,
            "land: merge, then remove worktree and branch",
            "l",
            f'run-shell -b "{land}"',
            "merge: local merge, no push, then remove worktree and branch",
            "m",
            f'run-shell -b "{land} --merge"',
            "auto land: land, the agent fixes what stops it",
            "a",
            f'run-shell -b "{land} --auto"',
        ]
    )
    # In place of tmux's own `r`, which redraws the screen.
    cmd(["bind-key", "r", "run-shell", "-b", review])


def agent_pane_id(session: str) -> str:
    # -s: every window of the session. Without it tmux lists the window in
    # front only, and when hunk quits that is the review window.
    fmt = "#{pane_id} #{" + ROLE_OPTION + "}"
    lines = out(["list-panes", "-s", "-t", f"={session}", "-F", fmt])
    panes: dict[str, str] = {}
    for line in lines.splitlines():
        pane_id, _, role = line.partition(" ")
        panes.setdefault(role.strip(), pane_id)
    # "claude" is what the old bash scripts tagged the pane.
    return panes.get("agent") or panes.get("claude", "")


def render_ctx(
    ctx: Ctx,
    resolved: list,
    *,
    llm: str = "",
    phase: str = "",
    effort: str = "",
) -> RenderContext:
    """What a backend needs to write settings and start, from a live context."""
    from . import notify  # notify imports tmux

    return RenderContext(
        root=ctx.root,
        main=ctx.main,
        branch=ctx.branch,
        session=ctx.session,
        cfg=ctx.cfg,
        repos=resolved,
        llm=llm or ctx.llm,
        phase=phase,
        plan_model=ctx.plan_model,
        plan_effort=ctx.plan_effort,
        effort_override=effort,
        conversation=notify.conversation(ctx.session) if phase == "build" else "",
    )


def pane_command(
    ctx: Ctx, pane: Pane, agent: Agent | None, rctx: RenderContext, *, brief: bool
) -> str:
    if pane.role == "agent" and agent is not None:
        return agent.launch_cmd(rctx, brief=brief)
    if pane.cmd:
        return pane.cmd
    return "clear"


def urls(ctx: Ctx) -> str:
    lines = [f"session : {ctx.session}   (prefix+s to switch)"]
    for family in ctx.cfg.ports.families:
        port = ctx.env.get(family.env_key) or str(family.main)
        host = "localhost" if family.name == "frontend" else "127.0.0.1"
        lines.append(f"{family.name:8}: http://{host}:{port}/")
    for spec in ctx.cfg.repos:
        path = ctx.env.get(spec.path_key)
        if path:
            branch = ctx.env.get(spec.branch_key)
            suffix = f" ({branch})" if branch else " (shared main checkout, read only)"
            lines.append(f"{spec.name:8}: {path}{suffix}")
    return "\n".join(lines)


def attach(session: str) -> None:
    if os.environ.get("TMUX"):
        cmd(["switch-client", "-t", f"={session}"])
    else:
        run(["tmux", "attach-session", "-t", f"={session}"], check=False)


def focus_session(session: str) -> bool:
    """Put the session in front of whoever is looking, from anywhere.

    attach() switches the client that runs it, and a notification worker or a
    `run-shell` job is not one: it has no terminal, so tmux has to guess which
    client was meant. This names the client that was used last instead, and
    works from outside tmux too. False when nobody is attached.

    #{client_activity} is the plain epoch number (t: is what formats it), so
    the lines sort by time.
    """
    if not available() or not has_session(session):
        return False
    clients = out(["list-clients", "-F", "#{client_activity} #{client_name}"]).splitlines()
    newest = sorted(line for line in clients if " " in line)
    if not newest:
        return False
    cmd(["switch-client", "-c", newest[-1].split(" ", 1)[1], "-t", f"={session}"])
    return True


def respawn_agent(ctx: Ctx, line: str, *, note: str = "") -> bool:
    """Restart the agent pane on a new command line.

    Nothing reaches an agent that is already running, so the pane is respawned.
    Nothing is lost either: the conversation it replaces stays resumable, and
    the handoff resumes exactly that one.
    """
    pane = agent_pane_id(ctx.session)
    if not pane:
        warn(
            f"no agent pane in {ctx.session} (older session?). Kill the session "
            "and run wtx go again"
        )
        return False
    load = envfile.load_snippet(ctx.cfg.owned_env_keys)
    if note:
        say(note)
    cmd(["respawn-pane", "-k", "-t", pane, "-c", str(ctx.root)])
    cmd(["send-keys", "-t", pane, f"{load}; {line}", "C-m"])
    cmd(["select-pane", "-t", pane])
    return True


def brief_phase(rctx: RenderContext) -> str:
    """What a brief starts: a plan when it starts in plan mode, else a build."""
    if rctx.phase == "build" or rctx.permission_mode != "plan":
        return "build"
    return "plan"


def send_brief(ctx: Ctx, agent: Agent, rctx: RenderContext) -> bool:
    """Restart the agent pane on the checkout's PROMPT.md."""
    from . import notify  # notify imports tmux

    notify.remember_phase(ctx.session, brief_phase(rctx))
    return respawn_agent(
        ctx,
        agent.launch_cmd(rctx, brief=True),
        note=f"briefing {ctx.session} on {rctx.model or 'the default model'}",
    )


def ensure_session(
    ctx: Ctx,
    agent: Agent,
    resolved: list,
    *,
    attach_after: bool = True,
    brief: bool = False,
    phase: str = "plan",
) -> None:
    """Create the session if it is not there, then optionally attach.

    `brief` says whether the agent pane starts on PROMPT.md, `phase` which
    side of the plan/build handoff that brief is on. A pane with no brief has
    no phase: it is a plain session.

    workmux builds the session. The panes ask wtx what to run when they
    start (pane.py), and the brief travels in a launch record.
    """
    from . import pane as pane_mod
    from . import workmux
    from .proc import which

    if which("tmux") is None:
        warn("tmux is not installed, no session created")
        return
    if not workmux.available():
        warn("workmux is not installed, no session created. See `wtx doctor`.")
        return
    state, err = server_status()
    if state not in ("up", "absent"):
        warn(f"cannot reach the tmux server, no session created. {server_remedy(state, err)}")
        return
    panes = list(ctx.cfg.panes.panes)
    if not panes:
        return

    ensure_server()
    scrub_environment(ctx.cfg.owned_env_keys)
    server_options()
    rctx = render_ctx(ctx, resolved, phase=phase if brief else "")

    if has_session(ctx.session):
        if brief and (ctx.root / PROMPT_FILE).is_file():
            send_brief(ctx, agent, rctx)
        print(urls(ctx))
        if attach_after:
            attach(ctx.session)
        return

    want_brief = brief and (ctx.root / PROMPT_FILE).is_file()
    pane_mod.write_launch(ctx.session, brief=want_brief, phase=phase)
    if want_brief:
        from . import notify  # notify imports tmux

        notify.remember_phase(ctx.session, brief_phase(rctx))
    try:
        workmux.write_config(ctx.main, ctx.cfg, ctx.repo_name)
        workmux.open_session(ctx.main, ctx.root, ctx.branch)
    except workmux.WorkmuxError as exc:
        pane_mod.clear_launch(ctx.session)
        warn(f"workmux could not open {ctx.session}: {exc}")
        return

    print(urls(ctx))
    if attach_after:
        attach(ctx.session)


def pipe_log(logs: Path, name: str, pane_id: str) -> None:
    """Mirror a server pane to a plain file.

    The agent runs sandboxed with no access to the tmux socket, so a log file
    inside the checkout is the only way it can see why a server misbehaves.
    """
    try:
        logs.mkdir(parents=True, exist_ok=True)
        (logs / f"{name}.log").write_text("")
    except OSError as exc:
        warn(f"cannot write {logs}: {exc}")
        return
    cmd(["pipe-pane", "-t", pane_id, "-o", f"exec cat >> '{logs}/{name}.log'"])
