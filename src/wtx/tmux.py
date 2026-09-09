"""The tmux session for a checkout.

One session per checkout, named "<repo>/<branch>", one window, the agent pane
filling the left half and the rest stacked on the right. Panes are addressed
through the @wt_role option, never by title: a coding agent rewrites its own
pane title as it works, and a title is not a tmux target anyway.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from . import envfile
from .agents.base import PROMPT_FILE, Agent, RenderContext
from .config import Pane
from .context import Ctx
from .proc import capture, capture_code, run, say, warn

STATE_OPTION = "@wtx_state"
ROLE_OPTION = "@wt_role"
WINDOW = "dev"


def available() -> bool:
    from .proc import which

    return which("tmux") is not None and server_reachable()


def _tmux(args: list[str], *, check: bool = False) -> int:
    return run(["tmux", *args], check=check, quiet=True)


def _tmux_out(args: list[str]) -> str:
    return capture(["tmux", *args])


def server_reachable() -> bool:
    """Can we talk to the tmux server at all?

    When the socket is unreachable, which is what an agent sandbox does, tmux
    still exits 0 and only complains on stderr. Believing the exit code makes
    every session look present, so wtx never creates one and quietly attaches to
    nothing.
    """
    code, _, err = capture_code(["tmux", "list-sessions"])
    if err and "no server running" in err.lower():
        return True
    return code in (0, 1) and not err


def server_up() -> bool:
    code, _, err = capture_code(["tmux", "has-session"])
    return code == 0 and not err


def has_session(name: str) -> bool:
    code, _, err = capture_code(["tmux", "has-session", "-t", f"={name}"])
    return code == 0 and not err


def list_sessions() -> list[str]:
    out = _tmux_out(["list-sessions", "-F", "#{session_name}"])
    return [line for line in out.splitlines() if line]


def set_session_option(session: str, key: str, value: str) -> None:
    _tmux(["set-option", "-t", f"={session}", key, value])


def get_session_option(session: str, key: str) -> str:
    return _tmux_out(["show-options", "-v", "-t", f"={session}", key])


def kill_session(session: str) -> None:
    if has_session(session):
        _tmux(["kill-session", "-t", f"={session}"])


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
        _tmux(["set-environment", "-gu", key])


def server_options() -> None:
    """Server-wide settings, refreshed on every run.

    detach-on-destroy off: killing a session moves the client to the next one
    instead of dropping out of tmux.

    prefix+X: a small menu on the current session. The binding is server wide
    and the last run owns it, whatever repo it came from, so it bakes in no
    path: wtx is on PATH and the branch comes from the pane's own directory.
    tmux parses the snippet before sh does, so it uses no dollar signs and no
    double quotes.
    """
    _tmux(["set-option", "-g", "detach-on-destroy", "off"])
    resolve = "wtx done --path '#{pane_current_path}' --from-tmux"
    close = f'run-shell -b "{resolve}"'
    discard = (
        "confirm-before -p 'discard uncommitted changes and remove the worktree? (y/n)' "
        f'"run-shell -b \\"{resolve} --force\\""'
    )
    _tmux(
        [
            "bind-key",
            "X",
            "display-menu",
            "-T",
            " #S ",
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
        ]
    )


def agent_pane_id(session: str) -> str:
    out = _tmux_out(
        ["list-panes", "-t", f"={session}", "-F", "#{pane_id} #{" + ROLE_OPTION + "}"]
    )
    for line in out.splitlines():
        pane_id, _, role = line.partition(" ")
        if role.strip() == "agent":
            return pane_id
    # sessions made by the old bash scripts tagged the pane "claude"
    for line in out.splitlines():
        pane_id, _, role = line.partition(" ")
        if role.strip() == "claude":
            return pane_id
    return ""


def _split_percent(position: int, total: int) -> int:
    """Size for the new pane so the right column ends up in equal parts."""
    remaining = total - position + 1
    return round(100 * remaining / (remaining + 1))


def render_ctx(
    ctx: Ctx, resolved: list, *, llm: str = "", phase: str = "", size: str = ""
) -> RenderContext:
    """What a backend needs to write settings and start, from a live context."""
    return RenderContext(
        root=ctx.root,
        main=ctx.main,
        branch=ctx.branch,
        session=ctx.session,
        cfg=ctx.cfg,
        repos=resolved,
        llm=llm or ctx.llm,
        phase=phase,
        size=size,
    )


def pane_command(ctx: Ctx, pane: Pane, agent: Agent, rctx: RenderContext, *, brief: bool) -> str:
    if pane.role == "agent":
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
        _tmux(["switch-client", "-t", f"={session}"])
    else:
        run(["tmux", "attach-session", "-t", f"={session}"], check=False)


def respawn_agent(ctx: Ctx, cmd: str, *, note: str = "") -> bool:
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
    _tmux(["respawn-pane", "-k", "-t", pane, "-c", str(ctx.root)])
    _tmux(["send-keys", "-t", pane, f"{load}; {cmd}", "C-m"])
    _tmux(["select-pane", "-t", pane])
    return True


def send_brief(ctx: Ctx, agent: Agent, rctx: RenderContext) -> bool:
    """Restart the agent pane on the checkout's PROMPT.md."""
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
) -> None:
    """Create the session if it is not there, then optionally attach."""
    if not available():
        from .proc import which

        if which("tmux") is None:
            warn("tmux is not installed, no session created")
        else:
            warn(
                "cannot reach the tmux server, no session created. "
                "A sandboxed agent cannot: run this from a real terminal."
            )
        return
    panes = list(ctx.cfg.panes.panes)
    if not panes:
        return

    scrub_environment(ctx.cfg.owned_env_keys)
    server_options()
    rctx = render_ctx(ctx, resolved, phase="plan" if brief else "")

    if has_session(ctx.session):
        if brief and (ctx.root / PROMPT_FILE).is_file():
            send_brief(ctx, agent, rctx)
        print(urls(ctx))
        if attach_after:
            attach(ctx.session)
        return

    agent_panes = [p for p in panes if p.role == "agent"]
    others = [p for p in panes if p.role != "agent"]
    ordered = agent_panes + others
    first = ordered[0]

    _tmux(
        [
            "new-session",
            "-d",
            "-s",
            ctx.session,
            "-n",
            WINDOW,
            "-c",
            str(ctx.root / first.cwd),
        ]
    )
    server_options()  # this run may have just started the server

    ids: list[str] = [
        _tmux_out(["display-message", "-p", "-t", f"={ctx.session}:{WINDOW}", "#{pane_id}"])
    ]
    rest = ordered[1:]
    for index, pane in enumerate(rest, start=1):
        target = ids[-1]
        direction = "-h" if index == 1 else "-v"
        size = 50 if index == 1 else _split_percent(index, len(rest))
        new_id = _tmux_out(
            [
                "split-window",
                direction,
                "-l",
                f"{size}%",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                target,
                "-c",
                str(ctx.root / pane.cwd),
            ]
        )
        ids.append(new_id)

    for pane_id, pane in zip(ids, ordered, strict=False):
        _tmux(["select-pane", "-t", pane_id, "-T", pane.name])
        _tmux(["set-option", "-p", "-t", pane_id, ROLE_OPTION, pane.role])
    _tmux(["set-option", "-w", "-t", f"={ctx.session}:{WINDOW}", "pane-border-status", "top"])

    logs = ctx.logs_dir
    load = envfile.load_snippet(ctx.cfg.owned_env_keys)
    want_brief = brief and (ctx.root / PROMPT_FILE).is_file()
    for pane_id, pane in zip(ids, ordered, strict=False):
        cmd = pane_command(ctx, pane, agent, rctx, brief=want_brief and pane.role == "agent")
        _tmux(["send-keys", "-t", pane_id, f"{load}; {cmd}", "C-m"])
        if pane.log:
            _pipe_log(logs, pane.name, pane_id)
    if ids:
        _tmux(["select-pane", "-t", ids[0]])

    print(urls(ctx))
    if attach_after:
        attach(ctx.session)


def _pipe_log(logs: Path, name: str, pane_id: str) -> None:
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
    _tmux(["pipe-pane", "-t", pane_id, "-o", f"exec cat >> '{logs}/{name}.log'"])
