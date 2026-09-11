"""opencode backend.

Writes <worktree>/opencode.json, the project-level config opencode reads. The
shapes differ from Claude Code but the intent is the same: a fixed baseline
wtx ships, plus what the repo's wtx.toml adds, plus one rule per external repo.

opencode's own permission keys are globs on the command string, and external
directories are governed by permission.external_directory rather than by a list
of extra readable directories.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from .base import PROMPT_FILE, PROMPT_SENT, RenderContext

SETTINGS_PATH = "opencode.json"
SCHEMA = "https://opencode.ai/config.json"


def _baseline() -> dict:
    text = (
        resources.files("wtx.templates.opencode").joinpath("permission.baseline.json").read_text()
    )
    return json.loads(text)


def _rule_to_glob(rule: str) -> str | None:
    """Turn a Claude-style Bash(...) rule into an opencode bash glob.

    Repos write their allow and deny lists once, in Claude's shape, and wtx
    translates. A rule that is not a Bash rule has no opencode equivalent and
    is skipped.
    """
    if rule.startswith("Bash(") and rule.endswith(")"):
        return rule[len("Bash(") : -1]
    return None


class OpencodeAgent:
    name = "opencode"
    binary = "opencode"

    def _model(self, ctx: RenderContext, model: str) -> str:
        provider = ctx.cfg.agent.opencode.provider
        if not model:
            return ""
        if "/" in model or not provider:
            return model
        return f"{provider}/{model}"

    def build_settings(self, ctx: RenderContext) -> dict:
        cfg = ctx.cfg
        permission = _baseline()
        bash = permission["bash"]

        for rule in cfg.permissions.allow:
            glob = _rule_to_glob(rule)
            if glob:
                bash[glob] = "allow"
        for rule in cfg.permissions.ask:
            glob = _rule_to_glob(rule)
            if glob:
                bash[glob] = "ask"
        # Protected branches, then the repo's own deny list. Deny is applied
        # last so a repo can tighten but the baseline still wins on what it set.
        for branch in cfg.repo.protected_branches:
            bash[f"git push * {branch}"] = "deny"
            bash[f"git push *:{branch}"] = "deny"
        for rule in cfg.permissions.deny:
            glob = _rule_to_glob(rule)
            if glob:
                bash[glob] = "deny"

        ext = permission["external_directory"]
        for r in ctx.read_repos:
            ext[f"{r.path}/**"] = "allow"
        for r in ctx.pair_repos:
            ext[f"{r.path}/**"] = "allow"

        if ctx.read_repos:
            edit = permission.get("edit")
            if not isinstance(edit, dict):
                edit = {"*": edit or "allow"}
            for r in ctx.read_repos:
                edit[f"{r.path}/**"] = "deny"
            permission["edit"] = edit

        out: dict = {"$schema": SCHEMA, "permission": permission}
        model = self._model(ctx, ctx.model)
        # When the plan is accepted opencode switches agents itself, and the
        # model follows the agent. There is no respawn to pass a build model
        # to, so with orchestration on the models ride the file, one per
        # agent under `agent`. The top level model stays the worktree's own
        # one: a plan brief must not change it, see docs/traps.md.
        orch = ctx.cfg.agent.orchestration
        plan_model = build_model = ""
        if orch.enabled:
            plan_model = self._model(ctx, ctx.plan_model or orch.plan_model)
            build_model = self._model(ctx, orch.build_model)
        if model:
            out["model"] = model
            out["agent"] = {
                cfg.agent.opencode.plan_agent: {"model": plan_model or model},
                cfg.agent.opencode.build_agent: {"model": build_model or model},
            }
        small = cfg.agent.opencode.small_model or cfg.agent.subagent_model
        small_full = self._model(ctx, small)
        if small_full:
            out["small_model"] = small_full
        return out

    def render_settings(self, ctx: RenderContext) -> list[Path]:
        target = ctx.root / SETTINGS_PATH
        payload = json.dumps(self.build_settings(ctx), indent=2) + "\n"
        try:
            target.write_text(payload)
        except OSError as exc:
            from ..proc import warn

            warn(f"could not write {target}: {exc}")
            return []
        return [target]

    def launch_cmd(self, ctx: RenderContext, *, brief: bool) -> str:
        """opencode's TUI takes no starting message, so a brief runs headless
        first and the TUI then continues that same session.

        With orchestration on there is no `-m` at all. `-m` is the first thing
        opencode reads when it picks a model, ahead of the config file, so a
        planner passed on the command line would stay the model for the whole
        session and accepting the plan would switch the agent and nothing else.
        The models sit in `agent` in opencode.json instead, and each agent
        brings its own. See docs/traps.md.
        """
        model = "" if ctx.cfg.agent.orchestration.enabled else self._model(ctx, ctx.model)
        flags = f" -m {model}" if model else ""
        if brief:
            variant = f" --variant {ctx.cfg.agent.effort}" if ctx.cfg.agent.effort else ""
            oc = ctx.cfg.agent.opencode
            agent = oc.build_agent if ctx.phase == "build" else oc.plan_agent
            agent_flag = f" --agent {agent}" if agent else ""
            return (
                f"mv {PROMPT_FILE} {PROMPT_SENT} && "
                f'opencode run{flags}{agent_flag}{variant} "$(cat {PROMPT_SENT})"; '
                f"opencode{flags} -c"
            )
        return f"opencode{flags} -c || opencode{flags}"

    def handoff_cmd(self, ctx: RenderContext, *, session: str, prompt: str) -> str:
        """Empty, on purpose: no accepted-plan hook fires the handoff. opencode
        switches agents itself when the plan is accepted, and the build model
        follows the agent through `agent` in opencode.json, so there is no pane
        to respawn either. See `[agent.opencode]` in docs/schema.md."""
        return ""

    def hook_fragment(self) -> dict:
        """opencode signals its state through a plugin, not a settings hook.
        That plugin is the next piece of work."""
        return {}
