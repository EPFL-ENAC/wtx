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
    # Which side of the plan/build handoff this launch is on. Empty means a
    # plain session, which is neither. See orchestrate.py.
    phase: str = ""
    # What the planner called the job, on a build launch: small or large.
    size: str = ""
    # This worktree's own plan model and plan effort, from .env.worktree. Empty
    # means the repo config decides.
    plan_model: str = ""
    plan_effort: str = ""
    # The effort the planner asked for, on a build launch. It beats the size.
    effort_override: str = ""

    @property
    def read_repos(self) -> list[Resolved]:
        return [r for r in self.repos if r.exists and not r.paired]

    @property
    def pair_repos(self) -> list[Resolved]:
        return [r for r in self.repos if r.exists and r.paired]

    @property
    def model(self) -> str:
        """The model this launch runs on.

        A plan brief runs on the planning model whatever the worktree's own
        model is: that is the point of the handoff. Everything else, the
        settings file included, keeps the worktree's model.
        """
        orch = self.cfg.agent.orchestration
        if self.phase == "plan" and orch.enabled:
            planner = self.plan_model or orch.plan_model
            if planner:
                return planner
        return self.llm or self.cfg.agent.llm

    @property
    def permission_mode(self) -> str:
        """The mode a brief starts in. A build brief is past the plan."""
        cfg = self.cfg.agent
        if self.phase == "build":
            return cfg.orchestration.build_permission_mode
        return cfg.brief_permission_mode

    @property
    def effort(self) -> str:
        """How hard the agent works.

        Planning and implementing are not the same job. Planning is reading and
        thinking, so it runs low unless the caller said the plan is a big one.
        Implementing an accepted plan takes the effort the planner asked for.
        That is the lever the plan routes, not the model: a smaller model is
        the bigger bet and stays opt-in.
        """
        orch = self.cfg.agent.orchestration
        if self.phase == "plan" and orch.enabled:
            return self.plan_effort or orch.plan_effort or self.cfg.agent.effort
        if self.phase == "build" and orch.enabled:
            if self.effort_override:
                return self.effort_override
            if self.size:
                return orch.small_effort if self.size == "small" else orch.large_effort
        return self.cfg.agent.effort


class Agent(Protocol):
    name: str
    binary: str

    def render_settings(self, ctx: RenderContext) -> list[Path]:
        """Write the agent's per-worktree config. Returns the files written."""
        ...

    def launch_cmd(self, ctx: RenderContext, *, brief: bool) -> str:
        """The shell line the agent pane runs."""
        ...

    def handoff_cmd(self, ctx: RenderContext, *, session: str, prompt: str) -> str:
        """The shell line that continues conversation `session` on this ctx.

        Empty when the backend cannot resume a conversation by id, which is
        what orchestration is built on.
        """
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
