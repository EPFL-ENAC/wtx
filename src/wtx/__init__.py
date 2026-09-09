"""wtx: one branch, one worktree, one tmux session, one coding agent."""

from __future__ import annotations

__all__ = ["__version__"]


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("wtx")
    except Exception:  # pragma: no cover - source checkout without install
        return "0.1.0"


__version__ = _version()
