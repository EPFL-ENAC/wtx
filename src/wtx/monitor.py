"""One screen showing every agent session and which one is waiting for you.

The board reads the state files, not tmux, so a session that has not fired a
hook yet still shows up, and a state survives a tmux restart. It runs in its own
session called wtx-monitor.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from . import envfile, git, notify, tmux
from .proc import run, say, warn

SESSION = "wtx-monitor"
WINDOW = "board"
GRID_WINDOW = "grid"

ORDER = {"permission": 0, "idle": 1, "stop": 2, "": 3}


def _age(since: float) -> str:
    if not since:
        return ""
    seconds = int(max(0, time.time() - since))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"


def _rows() -> list[dict]:
    states = notify.all_states()
    sessions = tmux.list_sessions() if tmux.available() else []
    rows: list[dict] = []
    for name in sessions:
        if name == SESSION:
            continue
        data = states.get(name, {})
        rows.append(
            {
                "session": name,
                "state": data.get("state", ""),
                "since": data.get("since", 0),
                "message": data.get("message", ""),
                "live": True,
            }
        )
    for name, data in states.items():
        if sessions and name in sessions:
            continue
        if not sessions:
            rows.append(
                {
                    "session": name,
                    "state": data.get("state", ""),
                    "since": data.get("since", 0),
                    "message": data.get("message", ""),
                    "live": False,
                }
            )
    rows.sort(key=lambda r: (ORDER.get(r["state"], 3), r["session"]))
    return rows


def _ports_for(session: str) -> str:
    """Ports of the checkout behind a session, read from its agent pane's path."""
    if not tmux.available():
        return ""
    path = tmux._tmux_out(
        ["list-panes", "-t", f"={session}", "-F", "#{pane_current_path}"]
    ).splitlines()
    if not path:
        return ""
    top = git.toplevel(Path(path[0]))
    if top is None:
        return ""
    values = envfile.read_worktree(top)
    ports = [v for k, v in sorted(values.items()) if k.endswith("_PORT") and v.isdigit()]
    return " ".join(ports)


def render(width: int = 100) -> str:
    rows = _rows()
    header = f"wtx monitor   {time.strftime('%H:%M:%S')}   q quit, r refresh, 1-9 jump"
    lines = [header, "-" * min(width, 100)]
    if not rows:
        lines.append("no sessions. Start one with: wtx go <branch>")
        return "\n".join(lines)
    for index, row in enumerate(rows, start=1):
        icon = notify.EMOJI.get(row["state"], "  ")
        key = str(index) if index < 10 else " "
        state = row["state"] or ("running" if row["live"] else "gone")
        age = _age(row["since"])
        ports = _ports_for(row["session"]) if row["live"] else ""
        line = f"{key} {icon} {row['session']:<38} {state:<11} {age:>5}  {ports}"
        lines.append(line.rstrip())
        if row["message"]:
            message = row["message"].splitlines()[0][: width - 8]
            lines.append(f"      {message}")
    waiting = [r for r in rows if r["state"] in notify.WAITING]
    lines.append("-" * min(width, 100))
    lines.append(
        f"{len(waiting)} waiting for you, {len(rows)} session(s) total"
        if waiting
        else f"{len(rows)} session(s), none waiting"
    )
    return "\n".join(lines)


def jump(index: int) -> None:
    rows = _rows()
    if 1 <= index <= len(rows):
        target = rows[index - 1]["session"]
        if tmux.available() and tmux.has_session(target):
            tmux.attach(target)


def serve(interval: float = 2.0) -> int:
    """The loop that runs inside the monitor pane."""
    import select
    import termios
    import tty

    fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    old = termios.tcgetattr(fd) if fd is not None else None
    if fd is not None:
        tty.setcbreak(fd)
    try:
        while True:
            width = shutil.get_terminal_size((100, 40)).columns
            sys.stdout.write("\x1b[H\x1b[2J" + render(width) + "\n")
            sys.stdout.flush()
            if fd is None:
                time.sleep(interval)
                continue
            ready, _, _ = select.select([sys.stdin], [], [], interval)
            if not ready:
                continue
            key = sys.stdin.read(1)
            if key in ("q", "\x03", "\x04"):
                return 0
            if key.isdigit() and key != "0":
                jump(int(key))
    finally:
        if fd is not None and old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def open_board(*, grid: bool = False, interval: float = 2.0) -> int:
    """Create or attach the monitor session."""
    if not tmux.available():
        print(render())
        return 0
    if not tmux.has_session(SESSION):
        wtx = shutil.which("wtx") or f"{sys.executable} -m wtx"
        run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                SESSION,
                "-n",
                WINDOW,
                f"{wtx} monitor --serve --interval {interval}",
            ],
            check=False,
        )
        tmux.server_options()
    if grid:
        _build_grid()
    tmux.attach(SESSION)
    return 0


def _build_grid(limit: int = 6) -> None:
    """A window of read-only attaches, so every agent pane is visible at once.

    Read-only on purpose: this is for watching, and a nested attach that can
    type would fight with the real client.
    """
    sessions = [s for s in tmux.list_sessions() if s != SESSION][:limit]
    if not sessions:
        warn("no sessions to show in the grid")
        return
    if tmux._tmux_out(["list-windows", "-t", f"={SESSION}", "-F", "#{window_name}"]).count(
        GRID_WINDOW
    ):
        tmux._tmux(["kill-window", "-t", f"={SESSION}:{GRID_WINDOW}"])
    first = sessions[0]
    tmux._tmux(
        [
            "new-window",
            "-t",
            f"={SESSION}",
            "-n",
            GRID_WINDOW,
            f"TMUX= tmux attach -r -t '={first}'",
        ]
    )
    for name in sessions[1:]:
        tmux._tmux(
            [
                "split-window",
                "-t",
                f"={SESSION}:{GRID_WINDOW}",
                f"TMUX= tmux attach -r -t '={name}'",
            ]
        )
    tmux._tmux(["select-layout", "-t", f"={SESSION}:{GRID_WINDOW}", "tiled"])
    say(f"grid shows {len(sessions)} session(s), read only")
