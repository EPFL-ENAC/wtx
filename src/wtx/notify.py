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
import re
import shlex
import signal
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

# How a waiting session is painted: a tmux style for the picker, an SGR code
# for the panes wtx draws itself. Only the states that wait on you get a
# colour. A picker where every line is coloured says nothing.
TMUX_STYLE = {
    "permission": "fg=yellow,bold",
    "idle": "fg=cyan",
}
SGR = {
    "permission": "1;33",
    "idle": "36",
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


def settle(session: str) -> None:
    """The session stopped waiting: drop the state, the mark and the banner.

    Closing the banner is what keeps the desktop list short. Left alone, every
    permission prompt of the day piles up in it.
    """
    clear_state(session)
    close_desktop(session)
    if tmux.available():
        tmux.set_session_option(session, tmux.STATE_OPTION, "")


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
    out = tmux._tmux_out(["list-panes", "-a", "-F", "#{session_name}\t#{pane_current_path}"])
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
            settle(session)
        return 0

    if session and state in ("stop", "idle"):
        # An accepted plan waiting for its implementation model: the session is
        # not waiting on the human, it is about to start work.
        from . import orchestrate

        if orchestrate.drain(session):
            settle(session)
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


# ---------------------------------------------------------------------------
# the desktop notification

NOTIFY_DEST = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"

# Why a notification closed, from the freedesktop spec: 1 expired, 2 the user
# closed it, 3 CloseNotification was called, 4 undefined.
CLOSED_BY_USER = 2

# The terminals wtx knows, in the order it tries them: the desktop file GNOME
# should raise when the banner is clicked, and the flag that runs a command.
TERMINALS = (
    ("gnome-terminal", "org.gnome.Terminal", "--"),
    ("ptyxis", "org.gnome.Ptyxis", "--"),
    ("kitty", "kitty", ""),
    ("alacritty", "Alacritty", "-e"),
    ("x-terminal-emulator", "", "-e"),
    ("xterm", "xterm", "-e"),
)


def id_file(session: str) -> Path:
    return state_dir() / f"{safe_name(session)}.id"


def read_id(session: str) -> tuple[str, int]:
    """The notification id and the worker pid last written for a session."""
    try:
        parts = id_file(session).read_text().split()
    except OSError:
        return "", 0
    ident = parts[0] if parts and parts[0].isdigit() else ""
    pid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return ident, pid


def _is_worker(pid: int) -> bool:
    """Is that pid still one of our workers?

    pids are reused, and a worker is killed by process group, so acting on a
    stale number would take down an unrelated group and its children with it.
    """
    if pid <= 0:
        return False
    try:
        return b"wtx.notify" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def _stop_worker(pid: int) -> None:
    """Kill a worker and the bus monitor it is waiting on.

    The worker is started with start_new_session, so its pid is also its
    process group: killing the group gets the gdbus child too.
    """
    if not _is_worker(pid):
        return
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)


def close_desktop(session: str) -> None:
    """Take a session's notification out of the desktop list.

    Without this every permission prompt leaves a banner behind. A day of
    agents fills the GNOME list, and a full list makes the shell crawl.
    """
    ident, pid = read_id(session)
    _stop_worker(pid)
    if ident and which("gdbus") is not None:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # noqa: S603
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    NOTIFY_DEST,
                    "--object-path",
                    NOTIFY_PATH,
                    "--method",
                    f"{NOTIFY_DEST}.CloseNotification",
                    ident,
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
    with contextlib.suppress(OSError):
        id_file(session).unlink(missing_ok=True)


def _desktop_entry() -> str:
    """The .desktop name the notification points at.

    Guessed from the terminals on PATH, which is wrong on a machine that has
    more than one installed. WTX_DESKTOP_ENTRY settles it.
    """
    forced = os.environ.get("WTX_DESKTOP_ENTRY", "").strip()
    if forced:
        return forced
    for name, entry, _ in TERMINALS:
        if entry and which(name):
            return entry
    return ""


def _text(session: str, state: str, message: str) -> tuple[str, str]:
    icon = EMOJI.get(state, "")
    title = f"{icon} {session}".strip()
    body = message or {
        "permission": "waiting for permission",
        "idle": "waiting for you",
        "stop": "finished",
    }.get(state, state)
    return title, body


def _send(session: str, state: str, message: str, replace: str) -> str:
    """Show the banner and return its id.

    It carries no action on purpose. GNOME only raises the app named by
    desktop-entry when the notification has no default action: with one it
    emits ActionInvoked and nothing else, which is why clicking used to leave
    you on the wrong workspace. The click is caught on the bus instead, see
    _clicked.
    """
    title, body = _text(session, state, message)
    args = ["notify-send", "-p", "-a", "wtx"]
    if replace:
        args += ["-r", replace]
    entry = _desktop_entry()
    if entry:
        args += ["-h", f"string:desktop-entry:{entry}"]
    if state not in WAITING:
        # A finished run is worth a banner, not a line in the list for ever.
        args += ["-e"]
    args += [title, body]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    except OSError:
        return ""
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    return lines[0] if lines and lines[0].isdigit() else ""


def closed_reason(line: str, ident: str) -> int:
    """Why our notification closed, from one line of `gdbus monitor`.

    The line looks like:
      /org/freedesktop/Notifications: org.freedesktop.Notifications\
      .NotificationClosed (uint32 7, uint32 2)

    0 means the line is about something else.
    """
    if "NotificationClosed" not in line:
        return 0
    numbers = re.findall(r"uint32 (\d+)", line)
    if len(numbers) != 2 or numbers[0] != ident:
        return 0
    return int(numbers[1])


def _start_monitor() -> subprocess.Popen | None:
    """Watch the bus before the banner is sent, not after.

    Started first so a click on a banner that is already on screen (a replaced
    notification) is not missed in the gap.
    """
    if which("gdbus") is None or os.environ.get("WTX_NOTIFY_CLICK") == "0":
        return None
    try:
        return subprocess.Popen(  # noqa: S603
            [
                "gdbus",
                "monitor",
                "--session",
                "--dest",
                NOTIFY_DEST,
                "--object-path",
                NOTIFY_PATH,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None


def _clicked(proc: subprocess.Popen, ident: str) -> bool:
    """Block until our notification closes. True when you closed it.

    GNOME sends the same reason for a click on the banner and for the x
    button, so both send you to the session. That is a fair trade: the session
    was waiting on you either way. WTX_NOTIFY_CLICK=0 turns it off.
    """
    if proc.stdout is None:
        return False
    for line in proc.stdout:
        reason = closed_reason(line, ident)
        if reason:
            return reason == CLOSED_BY_USER
    return False


def _end(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        proc.terminate()
        proc.wait(timeout=2)


def worker(session: str, state: str, message: str, cwd: str = "") -> int:
    """The half that outlives the hook: show the banner, act on a click.

    One worker per session at a time. A new one kills the one it replaces, or
    every replaced notification leaves a process waiting on an id that is
    already gone, and a click then wakes several at once.
    """
    previous_id, previous_pid = read_id(session)
    if previous_pid != os.getpid():
        _stop_worker(previous_pid)

    if which("gdbus") is None:
        return _worker_notify_send(session, state, message, cwd, previous_id)

    watcher = _start_monitor()
    try:
        ident = _send(session, state, message, previous_id)
        if not ident:
            return 0
        with contextlib.suppress(OSError):
            id_file(session).write_text(f"{ident} {os.getpid()}")
        if watcher is not None and _clicked(watcher, ident):
            _open_session(session, cwd)
    finally:
        _end(watcher)
    return 0


def _worker_notify_send(session: str, state: str, message: str, cwd: str, previous_id: str) -> int:
    """The fallback for a machine without gdbus.

    notify-send -A blocks until the click and prints the action name. It costs
    the focus: a notification that carries an action is not allowed to raise
    its app, so this only moves the tmux client.
    """
    title, body = _text(session, state, message)
    args = ["notify-send", "-p", "-a", "wtx", "-A", "default=Open session"]
    if previous_id:
        args[1:1] = ["-r", previous_id]
    args += [title, body]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    except OSError:
        return 0
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if lines and lines[0].isdigit():
        with contextlib.suppress(OSError):
            id_file(session).write_text(f"{lines[0]} {os.getpid()}")
    if "default" in proc.stdout:
        _open_session(session, cwd)
    return 0


def _open_terminal(args: list[str]) -> None:
    for name, _, flag in TERMINALS:
        if which(name):
            line = [name, *([flag] if flag else []), *args] if args else [name]
            subprocess.Popen(  # noqa: S603
                line,
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
    if not tmux.focus_session(session):
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
    parts = [f"{EMOJI.get(state, '')} {name}".strip() for _, name, state in waiting[:max_items]]
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
