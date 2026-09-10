"""Which session needs you.

A coding agent's hooks run in the agent's own process, outside the sandbox its
Bash tool calls run in, so they can reach the tmux socket and the desktop. That
is the whole trick: the agent itself cannot talk to tmux, its hooks can.

Two outputs per event:

- a state file under $XDG_RUNTIME_DIR/wtx, read by `wtx tmux-status` and by the
  monitor board, so the state survives and can be shown for every session at
  once;
- the tmux session option @wtx_state, so tmux format strings can show it in the
  picker without running anything.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import git, tmux
from .context import session_name
from .proc import which

STATES = ("permission", "idle", "stop", "start", "running")

ICONS = {
    "permission": "[!]",
    "idle": "[?]",
    "stop": "[ok]",
    "running": "",
    "start": "",
}

EMOJI = {
    "permission": "\N{CLOSED LOCK WITH KEY}",
    "idle": "\N{SPEECH BALLOON}",
    "stop": "\N{WHITE HEAVY CHECK MARK}",
}

# What the user has to act on. running and start are just noise on a board.
WAITING = ("permission", "idle")


def state_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"  # noqa: S108
    d = Path(base) / "wtx"
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "-" for c in name)


def state_file(session: str) -> Path:
    return state_dir() / f"{safe_name(session)}.json"


def read_state(session: str) -> dict:
    f = state_file(session)
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def all_states() -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        files = sorted(state_dir().glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("session"):
            out[data["session"]] = data
    return out


def write_state(session: str, state: str, message: str = "") -> None:
    payload = {
        "session": session,
        "state": state,
        "since": time.time(),
        "message": message[:400],
    }
    with contextlib.suppress(OSError):
        state_file(session).write_text(json.dumps(payload))


def clear_state(session: str) -> None:
    with contextlib.suppress(OSError):
        state_file(session).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# resolving which session a hook came from


def repo_name_for(main: Path) -> str:
    """The name the session was built from: wtx.toml's, or the origin URL's.

    Guessing from the URL alone gives a different session name in a repo that
    sets [repo].name, and then a hook cannot find its own session.
    """
    from . import config as config_mod

    path = main / config_mod.CONFIG_NAME
    if path.is_file():
        try:
            return config_mod.load(path).repo.name or git.repo_name(main)
        except config_mod.ConfigError:
            pass
    return git.repo_name(main)


def session_for(cwd: Path) -> str:
    """The session name for the directory a hook reported.

    First the name the branch implies, then, if there is no such session, a scan
    of every pane for one sitting in that directory.

    The branch is read from the directory, not from `WT_BRANCH`. That variable
    holds whatever the shell that started the pane exported, which is another
    worktree's branch as soon as an agent is launched from one. It is only a
    fallback for a checkout git cannot name (a detached head).
    """
    main = git.main_checkout(cwd)
    branch = ""
    if main is not None:
        branch = git.current_branch(cwd) or os.environ.get("WT_BRANCH", "")
    if main is not None and branch:
        name = session_name(repo_name_for(main), branch)
        if tmux.available() and tmux.has_session(name):
            return name
    if not tmux.available():
        return ""
    out = tmux._tmux_out(
        ["list-panes", "-a", "-F", "#{session_name}\t#{pane_current_path}"]
    )
    target = str(cwd.resolve())
    for line in out.splitlines():
        name, _, path = line.partition("\t")
        if path and Path(path).resolve(strict=False).as_posix() == target:
            return name
    return ""


def payload_from_stdin() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def handle(state: str, *, cwd: Path | None = None) -> int:
    """Called by the agent's hook. Reads the hook payload on stdin."""
    if state not in STATES:
        return 2
    payload = payload_from_stdin()
    where = Path(payload.get("cwd") or cwd or Path.cwd())
    session = session_for(where)
    message = str(payload.get("message", ""))

    if state in ("start", "running"):
        if session:
            clear_state(session)
            if tmux.available():
                tmux.set_session_option(session, tmux.STATE_OPTION, "")
        return 0

    if session and state in ("stop", "idle"):
        # An accepted plan waiting for its implementation model: the session is
        # not waiting on the human, it is about to start work.
        from . import orchestrate

        if orchestrate.drain(session):
            clear_state(session)
            if tmux.available():
                tmux.set_session_option(session, tmux.STATE_OPTION, "")
            return 0

    if session:
        write_state(session, state, message)
        if tmux.available():
            tmux.set_session_option(session, tmux.STATE_OPTION, state)
    # An agent outside any wtx session (a plain terminal, an editor) still
    # deserves the desktop notification. Its title is the directory, and a
    # click opens a terminal there.
    _spawn_desktop(session or where.name, state, message, cwd=where)
    return 0


def _spawn_desktop(session: str, state: str, message: str, *, cwd: Path) -> None:
    """Fork the desktop notification so the hook returns straight away."""
    if which("notify-send") is None:
        return
    cmd = [sys.executable, "-m", "wtx.notify", "--worker", session, state, message, str(cwd)]
    with contextlib.suppress(OSError):
        subprocess.Popen(  # noqa: S603
            cmd,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def worker(session: str, state: str, message: str, cwd: str = "") -> int:
    """The blocking half: show the notification, act on a click.

    notify-send -A blocks until the notification is clicked or closed, hence the
    separate process. The session is resolved again at click time, not now, so a
    stale notification still lands somewhere sensible.
    """
    icon = EMOJI.get(state, "")
    title = f"{icon} {session}".strip()
    body = message or {
        "permission": "waiting for permission",
        "idle": "waiting for you",
        "stop": "finished",
    }.get(state, state)

    id_file = state_dir() / f"{safe_name(session)}.id"
    args = ["notify-send", "-p", "-A", "default=Open session", title, body]
    previous = ""
    with contextlib.suppress(OSError):
        previous = id_file.read_text().strip()
    if previous:
        args[1:1] = ["-r", previous]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    except OSError:
        return 0
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if lines:
        with contextlib.suppress(OSError):
            id_file.write_text(lines[0].strip())
    if "default" not in proc.stdout:
        return 0
    # A replaced notification leaves the earlier worker waiting on the same
    # id, so one click can wake several. The first one acts.
    opened = id_file.with_suffix(".opened")
    now = int(time.time())
    with contextlib.suppress(OSError, ValueError):
        if now - int(opened.read_text().strip() or 0) < 3:
            return 0
    with contextlib.suppress(OSError):
        opened.write_text(str(now))
    _open_session(session, cwd)
    return 0


def _open_terminal(args: list[str]) -> None:
    for term in ("gnome-terminal", "x-terminal-emulator", "xterm"):
        if which(term):
            flag = "--" if term == "gnome-terminal" else "-e"
            subprocess.Popen(  # noqa: S603
                [term, flag, *args] if args else [term],
                start_new_session=True,
            )
            return


def _open_session(session: str, cwd: str = "") -> None:
    if not tmux.available() or not tmux.has_session(session):
        if cwd:
            _open_terminal(["sh", "-c", f"cd {shlex.quote(cwd)} && exec ${{SHELL:-sh}}"])
        return
    pane = tmux.agent_pane_id(session)
    if pane:
        tmux._tmux(["select-pane", "-t", pane])
    clients = tmux._tmux_out(
        ["list-clients", "-F", "#{client_activity} #{client_name}"]
    ).splitlines()
    if clients:
        newest = sorted(clients)[-1].split(" ", 1)
        if len(newest) == 2:
            tmux._tmux(["switch-client", "-c", newest[1], "-t", f"={session}"])
            return
    _open_terminal(["tmux", "attach-session", "-t", f"={session}"])


def status_line(max_items: int = 4) -> str:
    """One line for tmux status-right: the sessions waiting on you."""
    waiting = [
        (data.get("since", 0), name, data.get("state", ""))
        for name, data in all_states().items()
        if data.get("state") in WAITING
    ]
    if not waiting:
        return ""
    live = set(tmux.list_sessions()) if tmux.available() else set()
    waiting = [w for w in waiting if not live or w[1] in live]
    if not waiting:
        return ""
    waiting.sort(reverse=True)
    parts = [
        f"{EMOJI.get(state, '')} {name}".strip() for _, name, state in waiting[:max_items]
    ]
    more = len(waiting) - max_items
    if more > 0:
        parts.append(f"+{more}")
    return " ".join(parts)


def main(argv: list[str]) -> int:  # pragma: no cover - process entry point
    if argv and argv[0] == "--worker":
        session = argv[1] if len(argv) > 1 else ""
        state = argv[2] if len(argv) > 2 else ""
        message = argv[3] if len(argv) > 3 else ""
        cwd = argv[4] if len(argv) > 4 else ""
        return worker(session, state, message, cwd)
    return handle(argv[0] if argv else "idle")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
