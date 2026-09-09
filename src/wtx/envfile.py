"""Read and write a worktree's .env.worktree.

This file is the one source of per-worktree values: ports, the branch, the
paired external repos. Every tmux pane exports it.

The write is a merge, not a rewrite. The old bash setup did
`cat > .env.worktree <<ENV`, so any key a human or a repo hook added by hand was
silently dropped on the next setup run, including the automatic post_checkout
one. Keys wtx does not own are kept.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path

ENV_FILE = ".env.worktree"

HEADER = "# written by wtx. Values below the marker are yours, wtx keeps them."
FOREIGN_MARKER = "# --- keys wtx does not manage, preserved across setup runs"

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def parse(text: str) -> dict[str, str]:
    """Read KEY=VALUE lines. Good enough for a file we also write."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        try:
            parts = shlex.split(raw, comments=True)
        except ValueError:
            parts = [raw]
        out[key] = parts[0] if parts else ""
    return out


def read(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return parse(path.read_text())


def read_worktree(root: Path) -> dict[str, str]:
    return read(root / ENV_FILE)


def _fmt(key: str, value: str) -> str:
    return f"{key}={shlex.quote(value)}"


def render(owned: Mapping[str, str], foreign_lines: Sequence[str]) -> str:
    lines = [HEADER]
    for key, value in owned.items():
        lines.append(_fmt(key, str(value)))
    kept = [ln for ln in foreign_lines if ln.strip()]
    if kept:
        lines.append("")
        lines.append(FOREIGN_MARKER)
        lines.extend(kept)
    return "\n".join(lines) + "\n"


def _foreign_lines(text: str, owned_keys: Sequence[str]) -> list[str]:
    """Lines of an existing file that wtx must keep.

    After the first wtx write the foreign keys live below the marker, comments
    and all. Before that (a file from the old bash scripts) any line whose key
    wtx does not own is foreign.
    """
    if not text:
        return []
    lines = text.splitlines()
    if FOREIGN_MARKER in lines:
        idx = lines.index(FOREIGN_MARKER)
        return lines[idx + 1 :]
    owned = set(owned_keys)
    kept: list[str] = []
    pending: list[str] = []
    for line in lines:
        stripped = line.strip()
        m = _LINE.match(line)
        if m:
            if m.group(1) not in owned:
                kept.extend(pending)
                kept.append(line)
            pending = []
            continue
        if stripped.startswith("#"):
            # A comment belongs to the key under it. Hold it until we know
            # whether that key is one wtx manages.
            pending.append(line)
        elif not stripped:
            pending = []
    return kept


def write(path: Path, owned: Mapping[str, str], owned_keys: Sequence[str]) -> None:
    """Write the owned keys, keep everything else.

    owned_keys is the full list wtx manages, which may be wider than the keys
    it writes this time. A key wtx owns but does not set now is dropped, not
    treated as foreign.
    """
    old = path.read_text() if path.is_file() else ""
    foreign = _foreign_lines(old, owned_keys)
    path.write_text(render(owned, foreign))


def write_worktree(root: Path, owned: Mapping[str, str], owned_keys: Sequence[str]) -> None:
    write(root / ENV_FILE, owned, owned_keys)


def load_snippet(owned_keys: Sequence[str]) -> str:
    """The shell line every tmux pane runs.

    It exports .env.worktree when there is one, and unsets the keys when there
    is not, so a pane opened in the main checkout does not inherit a worktree's
    ports from the tmux server environment.
    """
    keys = " ".join(owned_keys)
    return (
        "set -a; "
        f'if [ -f "$(git rev-parse --show-toplevel)/{ENV_FILE}" ]; '
        f'then . "$(git rev-parse --show-toplevel)/{ENV_FILE}"; '
        f"else unset {keys}; fi; "
        "set +a"
    )
