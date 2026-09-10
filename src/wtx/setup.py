"""Make a worktree runnable. Idempotent, because it runs more than once.

wt fires post_create when a worktree is made and post_checkout when a branch is
checked out, and some builds fire both on create. Every step here checks whether
its work is already done, so running it twice changes nothing.

Everything it reads comes from the main checkout: the config, the templates, the
seed files. A feature branch must not be able to change the rules the agent
working on it runs under.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import envfile, git, guard, ports, repos, tmux
from .agents import base as agents
from .agents.base import RenderContext
from .config import WtxConfig
from .context import Ctx
from .proc import CommandError, run_shell, say, warn


class SetupError(Exception):
    pass


def _python_version(cfg: WtxConfig, main: Path) -> str:
    """The interpreter a uv step should pin.

    uv does not look up the tree for .python-version. Run from a subdirectory it
    picks the newest interpreter on the machine instead of the repo's, and a
    package with no wheel for that version then tries to build from source.
    """
    if not cfg.deps.python_version_file:
        return ""
    f = main / cfg.deps.python_version_file
    if not f.is_file():
        return ""
    return f.read_text().strip().splitlines()[0] if f.read_text().strip() else ""


def seed(ctx: Ctx) -> None:
    """Copy the gitignored files a checkout needs from the main checkout.

    Never overwrites: a worktree may have changed its own copy on purpose.
    """
    cfg = ctx.cfg
    for rel in cfg.seed.required:
        if not (ctx.main / rel).exists():
            raise SetupError(
                f"{rel} is missing from the main checkout {ctx.main}, "
                "a worktree cannot be seeded from it"
            )
    for rel in cfg.seed.copy:
        src, dst = ctx.main / rel, ctx.root / rel
        if dst.exists() or not src.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        say(f"seeded {rel}")
    for rel in cfg.seed.symlink:
        src, dst = ctx.main / rel, ctx.root / rel
        if dst.exists() or dst.is_symlink() or not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)
        say(f"linked {rel} -> {src}")


def write_env(
    ctx: Ctx,
    resolved: list[repos.Resolved],
    *,
    agent: str,
    llm: str,
    plan_model: str = "",
    plan_effort: str = "",
) -> dict[str, str]:
    """Ports and the rest of .env.worktree.

    Existing ports are reused so a restart never moves a running server. Keys
    wtx does not own, including anything a repo hook wrote, are preserved by
    envfile.write.
    """
    cfg = ctx.cfg
    existing = envfile.read_worktree(ctx.root)
    port_values: dict[str, int] = {}
    have_all = all(
        existing.get(f.env_key, "").isdigit() for f in cfg.ports.families
    )
    if cfg.ports.families:
        if have_all:
            port_values = {f.env_key: int(existing[f.env_key]) for f in cfg.ports.families}
        else:
            port_values = ports.branch_ports(
                cfg, ctx.main, ctx.repo_name, ctx.branch, exclude=ctx.root
            )

    owned: dict[str, str] = {
        "WT_BRANCH": ctx.branch,
        "WT_SLUG": ctx.slug,
        "WTX_AGENT": agent,
        "WTX_LLM": llm,
        # The plan side of orchestration, per worktree. It has to live here:
        # the handoff and every later setup rebuild the render context from
        # scratch, so a flag passed once to `wtx go` has nowhere else to stay.
        "WTX_PLAN_MODEL": plan_model,
        "WTX_PLAN_EFFORT": plan_effort,
    }
    owned.update({k: str(v) for k, v in port_values.items()})
    owned.update(cfg.env_extra)
    owned.update(repos.env_values(resolved))
    # Keys a repo hook computes: wtx never sets them, it carries them over.
    for key in cfg.env_computed:
        if key in existing:
            owned[key] = existing[key]
    envfile.write_worktree(ctx.root, owned, cfg.owned_env_keys)
    ctx.env = envfile.read_worktree(ctx.root)
    return owned


def run_deps(ctx: Ctx, resolved: list[repos.Resolved]) -> None:
    cfg = ctx.cfg
    pyver = _python_version(cfg, ctx.main)
    for step in cfg.deps.steps:
        cwd = ctx.root / step.cwd
        if step.if_missing and (ctx.root / step.if_missing).exists():
            continue
        cmd = step.run.replace("{python_version}", pyver)
        if "{python_version}" in step.run and not pyver:
            warn(f"no python version file, running: {cmd}")
        say(f"deps: {cmd}")
        try:
            run_shell(cmd, cwd=cwd)
        except CommandError as exc:
            raise SetupError(f"dependency step failed: {step.run} ({exc})") from exc
    for cmd in cfg.deps.post_install:
        # Best effort. A missing hook manager is a warning, not a failed setup.
        if run_shell(cmd, cwd=ctx.root, check=False) != 0:
            warn(f"post_install step failed, continuing: {cmd}")
    for r in resolved:
        ei = r.spec.editable_install
        if not ei or not r.exists:
            continue
        cmd = ei.run.replace("{path}", str(r.path)).replace("{python_version}", pyver)
        say(f"editable install of {r.spec.name}: {cmd}")
        if run_shell(cmd, cwd=ctx.root / ei.cwd, check=False) != 0:
            warn(f"editable install of {r.spec.name} failed, the packaged version stays")


def render_agent(ctx: Ctx, resolved: list[repos.Resolved]) -> None:
    try:
        agent = agents.get(ctx.agent_tool)
    except KeyError as exc:
        warn(str(exc))
        return
    rctx = RenderContext(
        root=ctx.root,
        main=ctx.main,
        branch=ctx.branch,
        session=ctx.session,
        cfg=ctx.cfg,
        repos=resolved,
        llm=ctx.llm,
        plan_model=ctx.plan_model,
        plan_effort=ctx.plan_effort,
    )
    for path in agent.render_settings(rctx):
        say(f"wrote {path.relative_to(ctx.root)}")


def run_setup(
    ctx: Ctx,
    *,
    with_repos: dict[str, str] | None = None,
    agent_tool: str = "",
    llm: str = "",
    plan_model: str = "",
    plan_effort: str = "",
    start_tmux: bool = True,
    attach: bool = False,
    brief: bool = False,
) -> None:
    """The whole thing, in the order the traps demand."""
    cfg = ctx.cfg
    if not ctx.is_worktree:
        warn(f"{ctx.root} is the main checkout, setting it up as one")

    # 0. Tracking repair. `wt create x origin/dev` leaves x tracking origin/dev,
    #    then `git pull` rebases the work onto dev and the next push is refused.
    git.repair_tracking(ctx.root, ctx.branch, cfg.repo.base_branch)

    # 1. Seeds.
    seed(ctx)

    # 2. External repos, including any pairing asked for or remembered.
    resolved = repos.resolve_all(ctx, requested=with_repos)

    # 3. Ports and .env.worktree.
    tool = agent_tool or ctx.agent_tool
    model = llm or ctx.llm
    # ctx.plan_model and ctx.plan_effort read .env.worktree first, so a value
    # passed once to `wtx go` survives every later setup with no flag.
    write_env(
        ctx,
        resolved,
        agent=tool,
        llm=model,
        plan_model=plan_model or ctx.env.get("WTX_PLAN_MODEL", ""),
        plan_effort=plan_effort or ctx.env.get("WTX_PLAN_EFFORT", ""),
    )

    # 4. The repo's own step, if it has one.
    if cfg.hooks.post_setup:
        say(f"hook post_setup: {cfg.hooks.post_setup}")
        if run_shell(cfg.hooks.post_setup, cwd=ctx.root, check=False) != 0:
            warn("post_setup hook failed")
        ctx.reload_env()

    # 5. Agent settings, always from the main checkout's config.
    render_agent(ctx, resolved)

    # 6. Dependencies, then the editable installs that pairing needs.
    run_deps(ctx, resolved)

    # 7. The push guard, after the dependency install so a hook manager
    #    installing its own hooks is not the one that replaces it.
    guard.install(cfg, ctx.main)

    # 8. The session.
    if start_tmux and cfg.panes.panes:
        try:
            agent = agents.get(ctx.agent_tool)
        except KeyError:
            return
        tmux.ensure_session(
            ctx, agent, resolved, attach_after=attach, brief=brief
        )
