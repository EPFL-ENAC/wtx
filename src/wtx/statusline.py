"""The Claude Code status line: model, context, branch, and the wtx phase.

Claude Code runs `wtx statusline` after each message and pipes a JSON payload
on stdin (model, effort, context window, repo, PR). This prints one line.

What the payload does not say, this reads itself:

- the auto-compact limit, from the same settings files Claude Code reads;
- the branch, and whether it is on the remote, from git;
- the wtx phase (plan, build, review), from the files wtx keeps per session.

It runs often and in front of the human, so it must be fast and must never
fail: any error gives a shorter line, never a traceback.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

ESC = "\033"
RESET = f"{ESC}[0m"
DIM = "2"
SEP = f" {ESC}[{DIM}m·{RESET} "

PHASE_COLOUR = {
    "plan": "36",  # cyan
    "build": "33",  # yellow
    "review": "35",  # magenta
}

# Claude Code's own range for autoCompactWindow.
MIN_WINDOW = 100_000


def colour(text: str, sgr: str) -> str:
    return f"{ESC}[{sgr}m{text}{RESET}" if sgr else text


def link(text: str, url: str) -> str:
    """An OSC 8 hyperlink. Ctrl+click opens it in a terminal that knows them."""
    if not url:
        return text
    return f"{ESC}]8;;{url}{ESC}\\{text}{ESC}]8;;{ESC}\\"


def tokens(n: int) -> str:
    """200000 -> 200k, 1000000 -> 1M."""
    if n >= 1_000_000:
        value = n / 1_000_000
        return f"{value:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1000:
        return f"{round(n / 1000)}k"
    return str(n)


# ---------------------------------------------------------------------------
# the auto-compact limit


def _read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def settings_files(project: Path) -> list[Path]:
    """Claude Code's settings files, the one that wins first."""
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return [
        Path("/etc/claude-code/managed-settings.json"),
        project / ".claude" / "settings.local.json",
        project / ".claude" / "settings.json",
        home / "settings.json",
    ]


def _window_in(data: dict, model_id: str) -> int | str | None:
    """The window one file sets for this model. A per-model value wins over
    the top-level one in the same file."""
    per_model = data.get("modelSettings")
    if isinstance(per_model, dict):
        entry = per_model.get(model_id)
        if isinstance(entry, dict) and "autoCompactWindow" in entry:
            return entry["autoCompactWindow"]
    return data.get("autoCompactWindow")


def compact_limit(project: Path, model_id: str, window: int) -> int | None:
    """At how many tokens Claude Code compacts. None when auto-compact is off.

    `auto`, or nothing set, means the model's own window.
    """
    # "claude-opus-5-5[1m]" is set as "claude-opus-5-5" in modelSettings.
    model_id = model_id.split("[", 1)[0]
    files = [_read_settings(p) for p in settings_files(project)]
    for data in files:
        if "autoCompactEnabled" in data:
            if data["autoCompactEnabled"] is False:
                return None
            break
    value: int | str | None = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or None
    if value is None:
        for data in files:
            value = _window_in(data, model_id)
            if value is not None:
                break
    with contextlib.suppress(TypeError, ValueError):
        if value is not None and str(value) != "auto":
            limit = max(int(value), MIN_WINDOW)
            return min(limit, window) if window else limit
    return window or None


# ---------------------------------------------------------------------------
# git and wtx


def _git(args: list[str], cwd: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=2
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return out.splitlines()


def branch_url(repo: dict, branch: str) -> str:
    """The branch page on the forge. Empty when the forge is not known."""
    host = repo.get("host", "")
    owner = repo.get("owner", "")
    name = repo.get("name", "")
    if not (host and owner and name and branch):
        return ""
    if "gitlab" in host:
        return f"https://{host}/{owner}/{name}/-/tree/{branch}"
    return f"https://{host}/{owner}/{name}/tree/{branch}"


def _tmux_session() -> str:
    """The tmux session this agent runs in. Empty outside tmux."""
    pane = os.environ.get("TMUX_PANE")
    if not pane or not os.environ.get("TMUX"):
        return ""
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.strip()


def wtx_phase(session: str, head: str) -> str:
    """plan, build or review. Empty for a plain session.

    Review wins: the branch has notes from `wtx review` that still match it,
    or the human sent notes back and the agent is working on them.
    """
    from . import notify, review

    with contextlib.suppress(OSError, PermissionError):
        if review.meta_file(session).is_file() and (
            review.status(session, head) == "ok" or review.sent_file(session).is_file()
        ):
            return "review"
        return notify.phase(session)
    return ""


def _start(main: Path, branch: str) -> str:
    """The commit the branch was cut from: the first line of its reflog."""
    log = main / ".git" / "logs" / "refs" / "heads" / branch
    with contextlib.suppress(OSError, IndexError), log.open() as fh:
        return fh.readline().split()[1]
    return ""


def _merged_by_a_merge(cwd: Path, head: str, ref: str) -> bool:
    """HEAD is a second parent of a merge in ref. Both roads of wtx land
    merge with --no-ff, so a landed branch always has one."""
    for line in _git(["log", "--merges", "--format=%P", f"{head}..{ref}"], cwd):
        if head in line.split()[1:]:
            return True
    return False


def land_state(cwd: Path, main: Path, branch: str, head: str, base: str) -> tuple[str, int]:
    """("landed", 0), ("ahead", n) or ("", 0) for a fresh branch.

    It reads only local refs, so a merge done on the forge shows after the
    next fetch. A squash merge on the forge is not seen: its commit is new.
    """
    counts = []
    for ref in (f"origin/{base}", base):
        out = _git(["rev-list", "--count", f"{ref}..{head}"], cwd)
        if out and out[0].isdigit():
            counts.append((int(out[0]), ref))
    if not counts or branch == base:
        return "", 0
    ahead, ref = min(counts)
    if ahead:
        return "ahead", ahead
    # Nothing ahead: either nothing was done yet, or it all went in.
    start = _start(main, branch)
    if start:
        return ("landed", 0) if start != head else ("", 0)
    return ("landed", 0) if _merged_by_a_merge(cwd, head, ref) else ("", 0)


def wtx_session(main: Path, branch: str) -> str:
    """The wtx session of this checkout, when the agent runs inside it."""
    from .config import CONFIG_NAME
    from .context import session_name
    from .notify import repo_name_for

    if not (main / CONFIG_NAME).is_file():
        return ""
    name = session_name(repo_name_for(main), branch)
    return name if _tmux_session() == name else ""


# ---------------------------------------------------------------------------
# the line


def render(data: dict) -> str:
    parts: list[str] = []
    workspace = data.get("workspace") or {}
    cwd = Path(workspace.get("current_dir") or data.get("cwd") or Path.cwd())
    project = Path(workspace.get("project_dir") or cwd)

    # The model and how hard it works.
    model = data.get("model") or {}
    name = model.get("display_name") or model.get("id") or "?"
    effort = (data.get("effort") or {}).get("level", "")
    head = colour(name, "1")
    if effort:
        head += f" {effort}"
    if data.get("fast_mode"):
        head += " fast"
    parts.append(head)

    # The context, against the limit where it compacts.
    ctx = data.get("context_window") or {}
    window = int(ctx.get("context_window_size") or 0)
    usage = ctx.get("current_usage") or {}
    used = sum(
        int(usage.get(k) or 0)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    if not used and ctx.get("used_percentage") and window:
        used = int(window * float(ctx["used_percentage"]) / 100)
    limit = compact_limit(project, str(model.get("id", "")), window) if window else None
    if limit:
        pct = round(100 * used / limit)
        sgr = "32" if pct < 60 else "33" if pct < 85 else "31"
        parts.append(f"{colour(f'{tokens(used)}/{tokens(limit)}', sgr)} {pct}% before compact")
    elif window:
        parts.append(f"{tokens(used)}/{tokens(window)} (no auto-compact)")

    # The branch, a link when the remote has it. And the PR, if one is open.
    lines = _git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir", "HEAD", "--abbrev-ref", "HEAD"],
        cwd,
    )
    main = branch = sha = ""
    if len(lines) == 3:
        main, sha, branch = str(Path(lines[0]).parent), lines[1], lines[2]
    if branch and branch != "HEAD":
        pushed = bool(
            _git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], cwd)
        )
        url = branch_url(workspace.get("repo") or {}, branch) if pushed else ""
        text = link(branch, url) if url else colour(branch, DIM)
        parts.append(f"⎇ {text}")
    pr = data.get("pr") or {}
    if pr.get("number") and pr.get("url"):
        parts.append(link(f"PR #{pr['number']}", pr["url"]))

    # wtx: which session, and where it is in plan, build, review.
    if main and branch:
        with contextlib.suppress(Exception):
            session = wtx_session(Path(main), branch)
            if session:
                phase = wtx_phase(session, sha)
                label = f"wtx {phase}" if phase else "wtx"
                parts.append(colour(label, PHASE_COLOUR.get(phase, DIM)))
        # Is the work in the base branch yet? Any branch of a wtx repo.
        with contextlib.suppress(Exception):
            land = _land_part(cwd, Path(main), branch, sha)
            if land:
                parts.append(land)

    return SEP.join(parts)


def _land_part(cwd: Path, main: Path, branch: str, head: str) -> str:
    from .config import CONFIG_NAME
    from .config import load as load_config

    if not (main / CONFIG_NAME).is_file():
        return ""
    base = load_config(main / CONFIG_NAME).repo.base_branch
    state, ahead = land_state(cwd, main, branch, head, base)
    if state == "landed":
        return colour(f"✓ landed in {base}", "1;32")
    if state == "ahead":
        return colour(f"{ahead} to land in {base}", DIM)
    return ""


def main() -> int:
    try:
        raw = sys.stdin.read() if sys.stdin and not sys.stdin.isatty() else ""
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    try:
        line = render(data if isinstance(data, dict) else {})
    except Exception:  # noqa: BLE001 - a broken status line is worse than a short one
        line = ""
    print(line)
    return 0
