"""Claude Code backend.

Writes <worktree>/.claude/settings.local.json and, when asked, an Explore
subagent that searches on a small model.

The settings are always built from the main checkout's wtx.toml and from the
baseline shipped with wtx. A feature branch must not be able to change the
permissions the agent working on it runs under.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from .base import PROMPT_FILE, PROMPT_SENT, RenderContext

SETTINGS_PATH = ".claude/settings.local.json"
EXPLORE_PATH = ".claude/agents/Explore.md"

COMMENT = (
    "Written by wtx from the main checkout's wtx.toml. Edits here are lost on the "
    "next setup run, change wtx.toml instead. deny = never; ask = always prompts, "
    "even in auto mode, so it holds only real write-to-the-internet forms (never a "
    "bare interpreter: auto mode edits files through heredocs and would prompt on "
    "every edit); allow = skip the classifier for routine commands. curl is outside "
    "the sandbox because the Linux sandbox cannot reach loopback, so the worktree's "
    "own dev servers are only reachable that way."
)

EXPLORE_AGENT = """---
name: Explore
description: Read-only search agent for broad fan-out searches across the repo. Use when answering means sweeping many files or directories and only the conclusion is needed, not the file contents.
model: {model}
tools: Bash, Glob, Grep, Read, NotebookRead, WebFetch, WebSearch, TodoWrite
---

You search the repository and report what you found. You never edit files.

Report back with file paths and line numbers, a short answer to the question
asked, and nothing else. Quote at most a few lines per file. If the answer is
not in the repository, say so instead of guessing.
"""


def _baseline() -> dict:
    text = (
        resources.files("wtx.templates.claude")
        .joinpath("settings.baseline.json")
        .read_text()
    )
    return json.loads(text)


def _dedup(items: list[str]) -> list[str]:
    out: list[str] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


def protected_branch_rules(branches: tuple[str, ...]) -> list[str]:
    """Deny pushing to a protected branch, in the spellings git accepts.

    This is the classifier layer. The real stop is the pre-push hook, which
    holds whatever the agent tries.
    """
    rules: list[str] = []
    for b in branches:
        rules += [f"Bash(git push * {b})", f"Bash(git push *:{b})"]
    return rules


class ClaudeAgent:
    name = "claude"
    binary = "claude"

    # -- settings --------------------------------------------------------
    def build_settings(self, ctx: RenderContext) -> dict:
        cfg = ctx.cfg
        data = _baseline()
        perms = data["permissions"]

        perms["allow"] = _dedup(list(perms["allow"]) + list(cfg.permissions.allow))
        perms["ask"] = _dedup(list(perms["ask"]) + list(cfg.permissions.ask))
        perms["deny"] = _dedup(
            list(perms["deny"])
            + protected_branch_rules(cfg.repo.protected_branches)
            + list(cfg.permissions.deny)
        )

        if cfg.permissions.allowed_domains:
            domains = data["sandbox"]["network"]["allowedDomains"]
            data["sandbox"]["network"]["allowedDomains"] = _dedup(
                list(domains) + list(cfg.permissions.allowed_domains)
            )

        extra_dirs: list[str] = []
        deny_write: list[str] = []
        allow_write: list[str] = []

        for r in ctx.read_repos:
            path = str(r.path)
            extra_dirs.append(path)
            # additionalDirectories grants read only. This makes the no-write
            # explicit at the classifier layer too. A deny on Edit also stops
            # file creation; Write path rules are ignored by Claude Code.
            perms["deny"].append(f"Edit(//{path.lstrip('/')}/**)")
            deny_write.append(f"{path}/**")

        for r in ctx.pair_repos:
            path = str(r.path)
            extra_dirs.append(path)
            perms["allow"].append(f"Edit(//{path.lstrip('/')}/**)")
            allow_write.append(path)

        if extra_dirs:
            perms["additionalDirectories"] = _dedup(extra_dirs)
        if deny_write:
            data["sandbox"].setdefault("filesystem", {})["denyWrite"] = _dedup(deny_write)
        if allow_write:
            # Only written when something is paired. The worktree itself is
            # listed first so this still works if a project-level list replaces
            # the machine-level one instead of extending it.
            fs = data["sandbox"].setdefault("filesystem", {})
            fs["allowWrite"] = _dedup([str(ctx.root), *allow_write])

        out: dict = {"_comment": COMMENT}
        if ctx.model:
            out["model"] = ctx.model
        env: dict[str, str] = {}
        if cfg.agent.subagent_model:
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = cfg.agent.subagent_model
        if env:
            out["env"] = env
        if cfg.agent.effort:
            out["effortLevel"] = cfg.agent.effort
        if cfg.agent.auto_compact_window:
            out["autoCompactWindow"] = cfg.agent.auto_compact_window
        if cfg.agent.disabled_mcp_servers:
            out["disabledMcpServers"] = list(cfg.agent.disabled_mcp_servers)
        out["permissions"] = perms
        out["sandbox"] = data["sandbox"]
        return out

    def render_settings(self, ctx: RenderContext) -> list[Path]:
        written: list[Path] = []
        target = ctx.root / SETTINGS_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.build_settings(ctx), indent=2) + "\n"
        tmp = target.with_suffix(".json.wtx-tmp")
        try:
            tmp.write_text(payload)
            tmp.replace(target)
            written.append(target)
        except OSError as exc:  # a sandboxed agent cannot write here, not fatal
            from ..proc import warn

            warn(f"could not write {target}: {exc}")
            tmp.unlink(missing_ok=True)

        model = ctx.cfg.agent.explore_agent_model
        if model:
            explore = ctx.root / EXPLORE_PATH
            explore.parent.mkdir(parents=True, exist_ok=True)
            try:
                explore.write_text(EXPLORE_AGENT.format(model=model))
                written.append(explore)
            except OSError as exc:
                from ..proc import warn

                warn(f"could not write {explore}: {exc}")
        return written

    # -- running ---------------------------------------------------------
    def launch_cmd(self, ctx: RenderContext, *, brief: bool) -> str:
        """What the agent pane runs.

        With a brief, rename it first so a session recreated later resumes the
        conversation instead of firing the same brief twice. Without one, resume
        the checkout's last conversation; a fresh worktree has none and claude
        exits nonzero, so a new conversation starts.
        """
        if brief:
            flags = ""
            if ctx.model:
                flags += f" --model {ctx.model}"
            mode = ctx.cfg.agent.brief_permission_mode
            if mode:
                flags += f" --permission-mode {mode}"
            if ctx.cfg.agent.effort:
                flags += f" --effort {ctx.cfg.agent.effort}"
            return (
                f"mv {PROMPT_FILE} {PROMPT_SENT} && "
                f'claude{flags} "$(cat {PROMPT_SENT})"'
            )
        flags = f" --model {ctx.model}" if ctx.model else ""
        return f"claude{flags} --continue || claude{flags}"

    def hook_fragment(self) -> dict:
        """Machine-level hooks. Project settings files cannot carry hooks, and
        these must run outside the Bash sandbox to reach tmux anyway."""
        def entry(matcher: str, arg: str) -> dict:
            return {
                "matcher": matcher,
                "hooks": [{"type": "command", "command": f"wtx notify {arg}"}],
            }

        return {
            "Notification": [
                entry("permission_prompt", "permission"),
                entry("idle_prompt", "idle"),
            ],
            "Stop": [entry("*", "stop")],
            "SessionStart": [entry("*", "start")],
            "UserPromptSubmit": [entry("*", "running")],
        }
