"""Everything a command needs to know about where it is.

Built once per command. Holds the main checkout, the worktree in hand (which
may be the main checkout itself), the branch, the config, and the values from
.env.worktree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import config as config_mod
from . import envfile, git
from .config import WtxConfig
from .proc import warn


class ContextError(Exception):
    pass


def session_name(repo: str, branch: str) -> str:
    """tmux session name for a checkout.

    tmux refuses . and : in a session name, so they become dashes. Slashes are
    fine and keep <repo>/<branch> readable in the picker.
    """
    return f"{repo}/{branch}".replace(".", "-").replace(":", "-")


@dataclass
class Ctx:
    cwd: Path
    main: Path
    root: Path
    branch: str
    repo_name: str
    cfg: WtxConfig
    env: dict[str, str] = field(default_factory=dict)

    @property
    def is_worktree(self) -> bool:
        return self.root.resolve() != self.main.resolve()

    @property
    def slug(self) -> str:
        return git.slug(self.branch)

    @property
    def session(self) -> str:
        return session_name(self.repo_name, self.branch)

    @property
    def logs_dir(self) -> Path:
        return self.root / ".wt-logs"

    @property
    def agent_tool(self) -> str:
        return self.env.get("WTX_AGENT") or self.cfg.agent.tool

    @property
    def llm(self) -> str:
        return self.env.get("WTX_LLM") or self.cfg.agent.llm

    @property
    def plan_model(self) -> str:
        """The model the plan brief runs on, this worktree's own or the repo's."""
        return self.env.get("WTX_PLAN_MODEL") or self.cfg.agent.orchestration.plan_model

    @property
    def plan_effort(self) -> str:
        """How hard the plan brief works. `wtx go --plan-effort` writes it."""
        return self.env.get("WTX_PLAN_EFFORT") or self.cfg.agent.orchestration.plan_effort

    def port(self, family_name: str) -> int | None:
        for f in self.cfg.ports.families:
            if f.name == family_name:
                raw = self.env.get(f.env_key)
                if raw and raw.isdigit():
                    return int(raw)
                return f.main if not self.is_worktree else None
        return None

    def reload_env(self) -> None:
        self.env = envfile.read_worktree(self.root)


def load(cwd: Path | None = None, *, root: Path | None = None) -> Ctx:
    """Build a context from a directory, defaulting to the current one."""
    here = (root or cwd or Path.cwd()).resolve()
    main = git.main_checkout(here)
    if main is None:
        raise ContextError(f"{here} is not inside a git repository")
    top = git.toplevel(here) or main
    cfg_path = main / config_mod.CONFIG_NAME
    if not cfg_path.is_file():
        raise ContextError(
            f"no {config_mod.CONFIG_NAME} in {main}. Run `wtx init` there first."
        )
    cfg = config_mod.load(cfg_path)
    for problem in config_mod.validate(cfg):
        warn(f"{config_mod.CONFIG_NAME}: {problem}")
    name = cfg.repo.name or git.repo_name(main)
    branch = git.current_branch(top)
    ctx = Ctx(
        cwd=here,
        main=main,
        root=top,
        branch=branch,
        repo_name=name,
        cfg=cfg,
        env=envfile.read_worktree(top),
    )
    return ctx


def for_worktree(main: Path, path: Path, branch: str, cfg: WtxConfig) -> Ctx:
    """Context for a worktree we are setting up, before it can describe itself."""
    name = cfg.repo.name or git.repo_name(main)
    return Ctx(
        cwd=path,
        main=main,
        root=path,
        branch=branch,
        repo_name=name,
        cfg=cfg,
        env=envfile.read_worktree(path),
    )
