"""Read and validate a repo's wtx.toml.

The consumer repo holds one tracked `wtx.toml`. Everything that differs between
repos lives there. This module turns it into frozen dataclasses with defaults
filled in, so the rest of wtx never touches raw dicts.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

CONFIG_NAME = "wtx.toml"
SCHEMA_VERSION = 1

# Keys wtx always writes into .env.worktree, on top of the port families,
# [env].extra and the external repo prefixes.
BASE_ENV_KEYS = (
    "WT_BRANCH",
    "WT_SLUG",
    "WTX_AGENT",
    "WTX_LLM",
    "WTX_PLAN_MODEL",
    "WTX_PLAN_EFFORT",
)


class ConfigError(Exception):
    """wtx.toml is missing, unreadable or invalid."""


def _expand(text: str, *, lab: str, repo: str) -> str:
    return text.replace("{lab}", lab).replace("{repo}", repo)


@dataclass(frozen=True)
class RepoCfg:
    name: str = ""
    lab: str = ""
    base_branch: str = "main"
    protected_branches: tuple[str, ...] = ("main",)


@dataclass(frozen=True)
class PortFamily:
    name: str
    main: int
    env: str = ""

    @property
    def env_key(self) -> str:
        return self.env or f"{self.name.upper()}_PORT"


@dataclass(frozen=True)
class PortsCfg:
    range_start: int = 18000
    step: int = 1000
    slots: int = 500
    families: tuple[PortFamily, ...] = ()

    def base_for(self, index: int) -> int:
        return self.range_start + index * self.step


@dataclass(frozen=True)
class SeedCfg:
    copy: tuple[str, ...] = ()
    symlink: tuple[str, ...] = ()
    required: tuple[str, ...] = ()


@dataclass(frozen=True)
class DepStep:
    run: str
    if_missing: str = ""
    cwd: str = "."


@dataclass(frozen=True)
class DepsCfg:
    python_version_file: str = ""
    steps: tuple[DepStep, ...] = ()
    post_install: tuple[str, ...] = ()


@dataclass(frozen=True)
class Pane:
    name: str
    role: str = "shell"  # agent | server | shell
    cwd: str = "."
    cmd: str = ""
    log: bool = False


@dataclass(frozen=True)
class PanesCfg:
    layout: str = "agent-left-half"
    panes: tuple[Pane, ...] = ()

    def by_role(self, role: str) -> tuple[Pane, ...]:
        return tuple(p for p in self.panes if p.role == role)

    @property
    def agent_pane(self) -> Pane | None:
        found = self.by_role("agent")
        return found[0] if found else None


@dataclass(frozen=True)
class OpencodeCfg:
    provider: str = ""
    small_model: str = ""
    plan_agent: str = "plan"
    build_agent: str = "build"


# The permission modes an agent may be started in. A typo here is a launch
# that fails after the pane is already respawned, so it is checked up front.
PERMISSION_MODES = (
    "default",
    "acceptEdits",
    "plan",
    "auto",
    "dontAsk",
    "bypassPermissions",
)


# The effort levels the agent accepts. Same reason as the permission modes.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultracode")


@dataclass(frozen=True)
class OrchestrationCfg:
    """Plan on one model, implement on another.

    Off by default: it changes what a brief does. See docs/schema.md.

    A plan the planner called small gets less effort, not a smaller model.
    Anthropic's own guidance is that effort is usually the better lever, so
    small_model is empty until someone has measured that it is enough here.
    """

    enabled: bool = False
    plan_model: str = "fable"
    # Planning is reading and thinking, not writing. Low is enough for most
    # briefs, and `wtx go --plan-effort high` raises it for the ones it is not.
    plan_effort: str = "low"
    build_model: str = "opus"
    small_model: str = ""
    small_effort: str = "medium"
    large_effort: str = "xhigh"
    # auto, not acceptEdits: the plan is already read and agreed, so stopping
    # the build to ask about every command puts the human back in the loop they
    # just stepped out of. The permission baseline is still what says no.
    build_permission_mode: str = "auto"


@dataclass(frozen=True)
class AgentCfg:
    tool: str = "claude"
    llm: str = ""
    brief_permission_mode: str = "plan"
    subagent_model: str = "sonnet"
    effort: str = ""
    auto_compact_window: int = 0
    disabled_mcp_servers: tuple[str, ...] = ()
    explore_agent_model: str = ""
    orchestration: OrchestrationCfg = field(default_factory=OrchestrationCfg)
    opencode: OpencodeCfg = field(default_factory=OpencodeCfg)


@dataclass(frozen=True)
class PermissionsCfg:
    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChecksCfg:
    lint: tuple[str, ...] = ()
    test: tuple[str, ...] = ()


@dataclass(frozen=True)
class EditableInstall:
    run: str
    cwd: str = "."


@dataclass(frozen=True)
class ExtRepo:
    """An external directory an agent may need: a data repo, a sibling library,
    the k8s GitOps config."""

    name: str
    path: str
    access: str = "read"  # read | pair
    base_branch: str = "main"
    env_prefix: str = ""
    editable_install: EditableInstall | None = None

    @property
    def prefix(self) -> str:
        if self.env_prefix:
            return self.env_prefix
        safe = "".join(c if c.isalnum() else "_" for c in self.name).upper()
        return f"WTX_REPO_{safe}"

    @property
    def path_key(self) -> str:
        return f"{self.prefix}_PATH"

    @property
    def branch_key(self) -> str:
        return f"{self.prefix}_BRANCH"

    def resolve(self, main_checkout: Path) -> Path:
        raw = self.path
        if raw.startswith("~"):
            return Path(raw).expanduser().resolve()
        p = Path(raw)
        if p.is_absolute():
            return p.resolve()
        return (main_checkout / raw).resolve()


@dataclass(frozen=True)
class HooksCfg:
    post_setup: str = ""
    pre_teardown: str = ""


@dataclass(frozen=True)
class WtxConfig:
    repo: RepoCfg = field(default_factory=RepoCfg)
    ports: PortsCfg = field(default_factory=PortsCfg)
    seed: SeedCfg = field(default_factory=SeedCfg)
    deps: DepsCfg = field(default_factory=DepsCfg)
    env_extra: dict[str, str] = field(default_factory=dict)
    env_computed: tuple[str, ...] = ()
    panes: PanesCfg = field(default_factory=PanesCfg)
    agent: AgentCfg = field(default_factory=AgentCfg)
    permissions: PermissionsCfg = field(default_factory=PermissionsCfg)
    checks: ChecksCfg = field(default_factory=ChecksCfg)
    repos: tuple[ExtRepo, ...] = ()
    hooks: HooksCfg = field(default_factory=HooksCfg)
    schema_version: int = SCHEMA_VERSION
    min_wtx_version: str = ""
    source: Path | None = None

    # -- derived ---------------------------------------------------------
    @property
    def owned_env_keys(self) -> tuple[str, ...]:
        """Every key wtx writes into .env.worktree.

        Anything else in that file was put there by a human or a repo hook and
        must survive a setup re-run.
        """
        keys: list[str] = list(BASE_ENV_KEYS)
        keys += [f.env_key for f in self.ports.families]
        keys += list(self.env_extra)
        keys += list(self.env_computed)
        for r in self.repos:
            keys += [r.path_key, r.branch_key]
        out: list[str] = []
        for k in keys:
            if k not in out:
                out.append(k)
        return tuple(out)

    @property
    def needs_uv_no_sync(self) -> bool:
        """uv re-syncs the venv before every `uv run`, which puts the locked
        wheel back over an editable install. Any repo doing an editable install
        needs UV_NO_SYNC=1."""
        return any(r.editable_install for r in self.repos)

    def repo_by_name(self, name: str) -> ExtRepo | None:
        for r in self.repos:
            if r.name == name:
                return r
        return None


# ---------------------------------------------------------------------------
# parsing


def _as_str_tuple(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of strings")
    return tuple(value)


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def parse(data: dict[str, Any], *, source: Path | None = None, default_name: str = "") -> WtxConfig:
    """Turn a parsed wtx.toml into a WtxConfig. Raises ConfigError.

    default_name fills [repo].name when the file has none. It must be known
    here, before {repo} is expanded: an expansion with an empty name turns
    `epfl-{lab}/{repo}` into the whole lab folder, and nothing can put the
    placeholder back afterwards.
    """
    repo_t = _table(data, "repo")
    repo = RepoCfg(
        name=repo_t.get("name", "") or default_name,
        lab=repo_t.get("lab", ""),
        base_branch=repo_t.get("base_branch", "main"),
        protected_branches=_as_str_tuple(
            repo_t.get("protected_branches", [repo_t.get("base_branch", "main")]),
            "[repo].protected_branches",
        ),
    )

    ports_t = _table(data, "ports")
    families = []
    for raw in ports_t.get("family", []) or []:
        if "name" not in raw or "main" not in raw:
            raise ConfigError("[[ports.family]] needs name and main")
        families.append(PortFamily(name=raw["name"], main=int(raw["main"]), env=raw.get("env", "")))
    ports = PortsCfg(
        range_start=int(ports_t.get("range_start", 18000)),
        step=int(ports_t.get("step", 1000)),
        slots=int(ports_t.get("slots", 500)),
        families=tuple(families),
    )

    seed_t = _table(data, "seed")
    seed = SeedCfg(
        copy=_as_str_tuple(seed_t.get("copy"), "[seed].copy"),
        symlink=_as_str_tuple(seed_t.get("symlink"), "[seed].symlink"),
        required=_as_str_tuple(seed_t.get("required"), "[seed].required"),
    )

    deps_t = _table(data, "deps")
    steps = []
    for raw in deps_t.get("step", []) or []:
        if "run" not in raw:
            raise ConfigError("[[deps.step]] needs run")
        steps.append(
            DepStep(
                run=raw["run"],
                if_missing=raw.get("if_missing", ""),
                cwd=raw.get("cwd", "."),
            )
        )
    deps = DepsCfg(
        python_version_file=deps_t.get("python_version_file", ""),
        steps=tuple(steps),
        post_install=_as_str_tuple(deps_t.get("post_install"), "[deps].post_install"),
    )

    env_t = _table(data, "env")
    env_extra_raw = env_t.get("extra", {})
    if not isinstance(env_extra_raw, dict):
        raise ConfigError("[env].extra must be a table")
    env_extra = {str(k): str(v) for k, v in env_extra_raw.items()}
    env_computed = _as_str_tuple(env_t.get("computed"), "[env].computed")

    panes_t = _table(data, "panes")
    pane_list = []
    for raw in panes_t.get("pane", []) or []:
        if "name" not in raw:
            raise ConfigError("[[panes.pane]] needs name")
        pane_list.append(
            Pane(
                name=raw["name"],
                role=raw.get("role", "shell"),
                cwd=raw.get("cwd", "."),
                cmd=raw.get("cmd", ""),
                log=bool(raw.get("log", False)),
            )
        )
    panes = PanesCfg(layout=panes_t.get("layout", "agent-left-half"), panes=tuple(pane_list))

    agent_t = _table(data, "agent")
    oc_t = _table(agent_t, "opencode")
    orch_t = _table(agent_t, "orchestration")
    agent = AgentCfg(
        tool=agent_t.get("tool", "claude"),
        llm=agent_t.get("llm", ""),
        brief_permission_mode=agent_t.get("brief_permission_mode", "plan"),
        subagent_model=agent_t.get("subagent_model", "sonnet"),
        effort=agent_t.get("effort", ""),
        auto_compact_window=int(agent_t.get("auto_compact_window", 0)),
        disabled_mcp_servers=_as_str_tuple(
            agent_t.get("disabled_mcp_servers"), "[agent].disabled_mcp_servers"
        ),
        explore_agent_model=agent_t.get("explore_agent_model", ""),
        orchestration=OrchestrationCfg(
            enabled=bool(orch_t.get("enabled", False)),
            plan_model=orch_t.get("plan_model", "fable"),
            plan_effort=orch_t.get("plan_effort", "low"),
            build_model=orch_t.get("build_model", "opus"),
            small_model=orch_t.get("small_model", ""),
            small_effort=orch_t.get("small_effort", "medium"),
            large_effort=orch_t.get("large_effort", "xhigh"),
            build_permission_mode=orch_t.get("build_permission_mode", "auto"),
        ),
        opencode=OpencodeCfg(
            provider=oc_t.get("provider", ""),
            small_model=oc_t.get("small_model", ""),
            plan_agent=oc_t.get("plan_agent", "plan"),
            build_agent=oc_t.get("build_agent", "build"),
        ),
    )

    perm_t = _table(data, "permissions")
    permissions = PermissionsCfg(
        allow=_as_str_tuple(perm_t.get("allow"), "[permissions].allow"),
        ask=_as_str_tuple(perm_t.get("ask"), "[permissions].ask"),
        deny=_as_str_tuple(perm_t.get("deny"), "[permissions].deny"),
        allowed_domains=_as_str_tuple(
            perm_t.get("allowed_domains"), "[permissions].allowed_domains"
        ),
    )

    checks_t = _table(data, "checks")
    checks = ChecksCfg(
        lint=_as_str_tuple(checks_t.get("lint"), "[checks].lint"),
        test=_as_str_tuple(checks_t.get("test"), "[checks].test"),
    )

    ext_repos = []
    for raw in data.get("repos", []) or []:
        if "name" not in raw or "path" not in raw:
            raise ConfigError("[[repos]] needs name and path")
        ei_raw = raw.get("editable_install")
        ei = None
        if ei_raw:
            if not isinstance(ei_raw, dict) or "run" not in ei_raw:
                raise ConfigError(f"[[repos]] {raw['name']}: editable_install needs a run key")
            ei = EditableInstall(run=ei_raw["run"], cwd=ei_raw.get("cwd", "."))
        ext_repos.append(
            ExtRepo(
                name=raw["name"],
                path=raw["path"],
                access=raw.get("access", "read"),
                base_branch=raw.get("base_branch", "main"),
                env_prefix=raw.get("env_prefix", ""),
                editable_install=ei,
            )
        )

    hooks_t = _table(data, "hooks")
    hooks = HooksCfg(
        post_setup=hooks_t.get("post_setup", ""),
        pre_teardown=hooks_t.get("pre_teardown", ""),
    )

    cfg = WtxConfig(
        repo=repo,
        ports=ports,
        seed=seed,
        deps=deps,
        env_extra=env_extra,
        env_computed=env_computed,
        panes=panes,
        agent=agent,
        permissions=permissions,
        checks=checks,
        repos=tuple(ext_repos),
        hooks=hooks,
        schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        min_wtx_version=str(data.get("min_wtx_version", "")),
        source=source,
    )
    return _post_process(cfg)


def _post_process(cfg: WtxConfig) -> WtxConfig:
    """Fill in what the config implies: {lab}/{repo} expansion, UV_NO_SYNC."""
    lab, name = cfg.repo.lab, cfg.repo.name
    repos = tuple(replace(r, path=_expand(r.path, lab=lab, repo=name)) for r in cfg.repos)
    env_extra = dict(cfg.env_extra)
    if any(r.editable_install for r in repos) and "UV_NO_SYNC" not in env_extra:
        env_extra["UV_NO_SYNC"] = "1"
    return replace(cfg, repos=repos, env_extra=env_extra)


def find_config(start: Path) -> Path | None:
    """Look for wtx.toml at the git main checkout, then walk up from start."""
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        f = candidate / CONFIG_NAME
        if f.is_file():
            return f
    return None


def load(path: Path) -> WtxConfig:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"no {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse(data, source=path, default_name=_default_name(path.parent))


def _default_name(main: Path) -> str:
    """The repo name when wtx.toml has none: from the origin URL, like the old
    scripts, so the port hash lands on the same numbers."""
    from . import git

    return git.repo_name(main)


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.strip().split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def validate(cfg: WtxConfig) -> list[str]:
    """Return a list of problems. Empty means the config is usable."""
    errs: list[str] = []
    if cfg.schema_version != SCHEMA_VERSION:
        errs.append(
            f"schema_version {cfg.schema_version} is not {SCHEMA_VERSION}, "
            "this wtx may not understand the file"
        )
    if not cfg.repo.name:
        errs.append("[repo].name is empty and could not be guessed")
    if cfg.min_wtx_version:
        from . import __version__

        if _version_tuple(__version__) < _version_tuple(cfg.min_wtx_version):
            errs.append(
                f"this repo needs wtx {cfg.min_wtx_version} or newer, "
                f"installed is {__version__}: uv tool upgrade wtx"
            )
    if not cfg.repo.base_branch:
        errs.append("[repo].base_branch is empty")
    if cfg.repo.base_branch not in cfg.repo.protected_branches:
        errs.append(
            f"[repo].protected_branches does not contain the base branch "
            f"'{cfg.repo.base_branch}', a worktree could push to it"
        )
    seen_env: set[str] = set()
    for f in cfg.ports.families:
        if f.env_key in seen_env:
            errs.append(f"two port families write the same env key {f.env_key}")
        seen_env.add(f.env_key)
        if f.main <= 0:
            errs.append(f"port family {f.name}: main must be a port number")
    if cfg.ports.slots <= 0:
        errs.append("[ports].slots must be positive")
    if cfg.agent.tool not in ("claude", "opencode"):
        errs.append(f"[agent].tool '{cfg.agent.tool}' is not claude or opencode")
    for key, mode in (
        ("[agent].brief_permission_mode", cfg.agent.brief_permission_mode),
        (
            "[agent.orchestration].build_permission_mode",
            cfg.agent.orchestration.build_permission_mode,
        ),
    ):
        if mode and mode not in PERMISSION_MODES:
            errs.append(f"{key} '{mode}' is not one of {', '.join(PERMISSION_MODES)}")
    orch = cfg.agent.orchestration
    for key, effort in (
        ("[agent].effort", cfg.agent.effort),
        ("[agent.orchestration].plan_effort", orch.plan_effort),
        ("[agent.orchestration].small_effort", orch.small_effort),
        ("[agent.orchestration].large_effort", orch.large_effort),
    ):
        if effort and effort not in EFFORT_LEVELS:
            errs.append(f"{key} '{effort}' is not one of {', '.join(EFFORT_LEVELS)}")
    if orch.enabled:
        for name, value in (
            ("plan_model", orch.plan_model),
            ("build_model", orch.build_model),
        ):
            if not value:
                errs.append(
                    f"[agent.orchestration] is enabled but {name} is empty, "
                    "there is nothing to hand the plan to"
                )
        # Both agent tools here can split the plan from the build. Claude
        # fires the handoff from its ExitPlanMode hook. opencode switches
        # agents itself when the plan is accepted, and the agents/opencode.py
        # settings carry one model per agent, so there is nothing to gate on
        # the tool.
    agent_panes = cfg.panes.by_role("agent")
    if cfg.panes.panes and not agent_panes:
        errs.append('no pane has role = "agent", the coding agent has nowhere to run')
    if len(agent_panes) > 1:
        errs.append('more than one pane has role = "agent"')
    names = [p.name for p in cfg.panes.panes]
    if len(names) != len(set(names)):
        errs.append("two panes share a name")
    for p in cfg.panes.panes:
        if p.role == "server" and not p.cmd:
            errs.append(f"pane {p.name} has role server but no cmd")
    seen_repo: set[str] = set()
    for r in cfg.repos:
        if r.name in seen_repo:
            errs.append(f"two [[repos]] are named {r.name}")
        seen_repo.add(r.name)
        if r.access not in ("read", "pair"):
            errs.append(f"[[repos]] {r.name}: access must be read or pair")
        if r.editable_install and r.access != "pair":
            errs.append(f'[[repos]] {r.name}: editable_install needs access = "pair"')
        if "{lab}" in r.path and not cfg.repo.lab:
            errs.append(f"[[repos]] {r.name}: path uses {{lab}} but [repo].lab is empty")
    for key in cfg.env_extra:
        if not key.replace("_", "").isalnum():
            errs.append(f"[env].extra key {key!r} is not a shell-safe name")
    return errs
