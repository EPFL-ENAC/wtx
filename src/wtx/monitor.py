"""One screen showing every agent session and which one is waiting for you.

Two views, both in a session of their own called wtx-monitor. The board is a
table: it reads the state files, not tmux, so a session that has not fired a
hook yet still shows up and a state survives a tmux restart. The grid is one
tile per session, each showing the tail of that session's agent pane, because
the agent pane is where the work actually happens.
"""

from __future__ import annotations

import re
import shlex
import shutil
import sys
import time
import unicodedata
from pathlib import Path

from . import envfile, git, notify, tmux
from .proc import run, say, warn

SESSION = "wtx-monitor"
WINDOW = "board"
GRID_WINDOW = "grid"

# How many tiles fit before a grid is unreadable, and how often a tile redraws.
# Every tile runs a capture-pane, so a short interval times nine sessions is
# real work for the tmux server.
GRID_LIMIT = 9
PEEK_INTERVAL = 1.0

ORDER = {"permission": 0, "idle": 1, "stop": 2, "": 3}

# CSI and the two-character escapes, so a captured line can be measured and cut
# by what it prints rather than by how many bytes it takes.
ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


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


def _wtx_cmd() -> str:
    """How a pane the tmux server starts can run wtx again."""
    return shutil.which("wtx") or f"{sys.executable} -m wtx"


def open_board(*, grid: bool = False, interval: float = 2.0) -> int:
    """Create or attach the monitor session."""
    if not tmux.available():
        print(render())
        return 0
    if not tmux.has_session(SESSION):
        run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                SESSION,
                "-n",
                WINDOW,
                f"{_wtx_cmd()} monitor --serve --interval {interval}",
            ],
            check=False,
        )
        tmux.server_options()
    if grid:
        _build_grid()
        tmux._tmux(["select-window", "-t", f"={SESSION}:{GRID_WINDOW}"])
    # prefix+g runs this from a `run-shell` job. That job has TMUX in its
    # environment but no terminal of its own, so `switch-client` with no -c has
    # to guess which client it meant. Name the client that was used last
    # instead, and only attach for real when nobody is attached at all.
    if not tmux.focus_session(SESSION):
        tmux.attach(SESSION)
    return 0


def _build_grid(limit: int = GRID_LIMIT) -> None:
    """One tile per live session, each peeking at that session's agent pane."""
    rows = [r for r in _rows() if r["live"]][:limit]
    if not rows:
        warn("no sessions to show in the grid")
        return
    windows = tmux._tmux_out(["list-windows", "-t", f"={SESSION}", "-F", "#{window_name}"])
    if GRID_WINDOW in windows.split():
        tmux._tmux(["kill-window", "-t", f"={SESSION}:{GRID_WINDOW}"])

    wtx = _wtx_cmd()
    target = f"={SESSION}:{GRID_WINDOW}"
    ids = [
        tmux._tmux_out(
            [
                "new-window",
                "-t",
                f"={SESSION}",
                "-n",
                GRID_WINDOW,
                "-P",
                "-F",
                "#{pane_id}",
                f"{wtx} monitor --peek {shlex.quote(rows[0]['session'])}",
            ]
        )
    ]
    for row in rows[1:]:
        ids.append(
            tmux._tmux_out(
                [
                    "split-window",
                    "-t",
                    target,
                    "-P",
                    "-F",
                    "#{pane_id}",
                    f"{wtx} monitor --peek {shlex.quote(row['session'])}",
                ]
            )
        )
        # Re-tile after every split. Past four panes tmux refuses the next
        # one with "no space for new pane" if the layout is left alone.
        tmux._tmux(["select-layout", "-t", target, "tiled"])

    tmux._tmux(["select-layout", "-t", target, "tiled"])
    tmux._tmux(["set-option", "-w", "-t", target, "pane-border-status", "top"])
    for pane_id, row in zip(ids, rows, strict=False):
        if pane_id:
            tmux._tmux(["select-pane", "-t", pane_id, "-T", row["session"]])
    say(f"grid shows {len(rows)} session(s), Enter on a tile to go there")


# ---------------------------------------------------------------------------
# one tile


def plain(text: str) -> str:
    """The text without its escape sequences."""
    return ESCAPE.sub("", text)


def _char_width(ch: str) -> int:
    """Columns one character takes on screen."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def clip(text: str, width: int) -> str:
    """Cut a line to width, counting only what it prints.

    The capture keeps the agent's colours. A plain slice can cut in the middle
    of an escape sequence, and the leftover then paints the rest of the tile.
    """
    if width <= 0:
        return ""
    out: list[str] = []
    seen = 0
    pos = 0

    def take(chunk: str) -> bool:
        nonlocal seen
        for ch in chunk:
            step = _char_width(ch)
            if seen + step > width:
                return False
            out.append(ch)
            seen += step
        return True

    for match in ESCAPE.finditer(text):
        if not take(text[pos : match.start()]):
            return "".join(out)
        out.append(match.group())
        pos = match.end()
    take(text[pos:])
    return "".join(out)


def render_peek(
    lines: list[str], *, session: str, state: str, age: str, rows: int, width: int
) -> str:
    """One tile: a header, then the bottom of the agent pane.

    The bottom, because that is where the prompt and the question it is asking
    are. Every line ends on a reset, or a colour left open by the cut runs on
    into the next one.
    """
    icon = notify.EMOJI.get(state, "")
    label = "  ".join(p for p in (f"{icon} {session}".strip(), state or "running", age) if p)
    style = notify.SGR.get(state, "")
    header = clip(label, width)
    if style:
        header = f"\x1b[{style}m{header}\x1b[0m"
    body = lines[-(rows - 1) :] if rows > 1 else []
    return "\n".join([header, *(f"{clip(line, width)}\x1b[0m" for line in body)])


def _capture(pane: str) -> list[str]:
    """The visible lines of a pane, colours kept, blank tail dropped.

    Without dropping the blanks, a pane whose prompt sits high leaves the tile
    showing nothing but empty rows.
    """
    out = tmux._tmux_out(["capture-pane", "-p", "-e", "-t", pane])
    lines = out.split("\n")
    while lines and not plain(lines[-1]).strip():
        lines.pop()
    return lines


def peek(session: str, interval: float = PEEK_INTERVAL) -> int:
    """The loop that runs inside one grid tile.

    capture-pane, not a nested `tmux attach`: an attach inside a tile drags the
    real session down to the size of the tile, and it shows the top of the
    window while the part worth reading is the prompt at the bottom.
    """
    import select
    import termios
    import tty

    fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    old = termios.tcgetattr(fd) if fd is not None else None
    if fd is not None:
        tty.setcbreak(fd)
    pane = ""
    looked = 0.0
    try:
        while True:
            now = time.time()
            if not pane or now - looked > 3:
                # A handoff respawns the agent pane, so its id changes under us.
                pane = tmux.agent_pane_id(session) if tmux.available() else ""
                looked = now
            size = shutil.get_terminal_size((80, 24))
            data = notify.read_state(session)
            frame = render_peek(
                _capture(pane) if pane else ["(no agent pane)"],
                session=session,
                state=data.get("state", ""),
                age=_age(data.get("since", 0)),
                rows=size.lines,
                width=size.columns,
            )
            sys.stdout.write("\x1b[H" + frame.replace("\n", "\x1b[K\r\n") + "\x1b[K\x1b[J")
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
            if key in ("\r", "\n", "o"):
                # This runs in a pane, so TMUX is set and attach() switches the
                # client that is looking at the grid.
                tmux.attach(session)
    finally:
        if fd is not None and old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
