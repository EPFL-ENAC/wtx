"""wtx: every branch gets its own worktree, ports, tmux session and coding agent."""

from __future__ import annotations

__all__ = ["__version__"]


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("wtx")
    except Exception:  # pragma: no cover - source checkout without install
        return "0.1.0"


__version__ = _version()
