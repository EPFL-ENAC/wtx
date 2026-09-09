"""The interface every coding agent backend implements.

wtx knows how to make a worktree, pick ports and build a tmux session. What it
does not know is how a given agent is configured or started. That lives here,
one class per tool, so adding a third agent later touches nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..config import WtxConfig
from ..repos import Resolved


@dataclass
class RenderContext:
    """Everything a backend needs to write its settings and start."""

    root: Path
    main: Path
    branch: str
    session: str
    cfg: WtxConfig
    repos: list[Resolved] = field(default_factory=list)
    llm: str = ""
    brief: bool = False

    @property
    def read_repos(self) -> list[Resolved]:
        return [r for r in self.repos if r.exists and not r.paired]

    @property
    def pair_repos(self) -> list[Resolved]:
        return [r for r in self.repos if r.exists and r.paired]

    @property
    def model(self) -> str:
        return self.llm or self.cfg.agent.llm


class Agent(Protocol):
    name: str
    binary: str

    def render_settings(self, ctx: RenderContext) -> list[Path]:
        """Write the agent's per-worktree config. Returns the files written."""
        ...

    def launch_cmd(self, ctx: RenderContext, *, brief: bool) -> str:
        """The shell line the agent pane runs."""
        ...

    def hook_fragment(self) -> dict:
        """Hooks for the user's machine-level config, used by install-machine."""
        ...


def get(name: str) -> Agent:
    from .claude import ClaudeAgent
    from .opencode import OpencodeAgent

    table = {"claude": ClaudeAgent(), "opencode": OpencodeAgent()}
    if name not in table:
        raise KeyError(f"unknown agent tool {name!r}, expected claude or opencode")
    return table[name]


PROMPT_FILE = "PROMPT.md"
PROMPT_SENT = "PROMPT.sent.md"
