"""Claude Code backend.

Writes <worktree>/.claude/settings.local.json and, when asked, an Explore
subagent that searches on a small model.

The settings are always built from the main checkout's wtx.toml and from the
baseline shipped with wtx. A feature branch must not be able to change the
permissions the agent working on it runs under.
"""

from __future__ import annotations

import json
import shlex
from importlib import resources
from pathlib import Path

from .base import PROMPT_FILE, PROMPT_SENT, RenderContext

SETTINGS_PATH = ".claude/settings.local.json"
EXPLORE_PATH = ".claude/agents/Explore.md"
# Loaded every session, same priority as .claude/CLAUDE.md, and not the repo's
# own CLAUDE.md, which is a human's file wtx must not touch.
RULES_PATH = ".claude/rules/wtx.md"

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


def worktree_rules(ctx: RenderContext) -> str:
    """What the agent cannot work out for itself.

    Its ports are picked per worktree, its servers run in tmux panes it cannot
    reach, and the only window it has into them is a log file. None of that is
    discoverable from the repository, so it is written down here. Kept short:
    this loads into every session.
    """
    from ..envfile import read_worktree

    env = read_worktree(ctx.root)
    is_main = ctx.root.resolve() == ctx.main.resolve()
    ports = []
    for f in ctx.cfg.ports.families:
        port = env.get(f.env_key) or (str(f.main) if is_main else "")
        if port:
            ports.append((f.name, port))
    logs = [p.name for p in ctx.cfg.panes.by_role("server") if p.log]
    if not ports and not logs:
        return ""

    out = [
        "# This worktree",
        "",
        f"Written by wtx. Branch `{ctx.branch}`, tmux session `{ctx.session}`.",
    ]
    if ports:
        first = ports[0][0]
        out += [
            "",
            "## Testing the app",
            "",
            "The dev servers are already running, in tmux panes you cannot see or",
            "reach. Do not start them again. They listen on ports picked for this",
            "worktree alone:",
            "",
        ]
        out += [f"- {name}: <http://127.0.0.1:{port}/>" for name, port in ports]
        out += [
            "",
            "`wtx curl <family> [path] [curl args]` is how you reach them. It fills",
            "in the port and never prompts, whatever the method:",
            "",
            "```sh",
            f"wtx curl {first} /api/health",
            f"wtx curl {first} /items -X POST -d @body.json",
            "```",
            "",
            "It is the way to test these servers. Plain `curl http://127.0.0.1:<port>/`",
            "works for a GET and prompts on anything past one, and nothing else here",
            "reaches a local port at all: a browser extension cannot open localhost,",
            "and neither can the Bash sandbox, which is why `wtx curl` runs outside it.",
        ]
    if logs:
        out += [
            "",
            "## Server logs",
            "",
            "The panes are mirrored to files you can read:",
            "",
        ]
        out += [f"- `.wt-logs/{name}.log`" for name in logs]
        out += [
            "",
            f"Read them with `tail -n 50 .wt-logs/{logs[0]}.log`. Never `tail -f`,",
            "and never `grep` them without a line limit: they grow while you work.",
        ]
    return "\n".join(out) + "\n"


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
        # ctx.effort, not cfg.agent.effort: the file has to say the same thing
        # as the --effort flag on the command line, or a `claude` started by
        # hand in this worktree runs at another level than the pane does.
        if ctx.effort:
            out["effortLevel"] = ctx.effort
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

        rules = worktree_rules(ctx)
        if rules:
            written += self._write(ctx.root / RULES_PATH, rules)

        model = ctx.cfg.agent.explore_agent_model
        if model:
            written += self._write(
                ctx.root / EXPLORE_PATH, EXPLORE_AGENT.format(model=model)
            )
        return written

    def _write(self, target: Path, text: str) -> list[Path]:
        """A file a sandboxed agent cannot write. Not being able to is a
        warning, not a failure: everything else about the worktree still works."""
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        except OSError as exc:
            from ..proc import warn

            warn(f"could not write {target}: {exc}")
            return []
        return [target]

    # -- running ---------------------------------------------------------
    def launch_cmd(self, ctx: RenderContext, *, brief: bool) -> str:
        """What the agent pane runs.

        With a brief, rename it first so a session recreated later resumes the
        conversation instead of firing the same brief twice. Without one, resume
        the checkout's last conversation; a fresh worktree has none and claude
        exits nonzero, so a new conversation starts.
        """
        if brief:
            return (
                f"mv {PROMPT_FILE} {PROMPT_SENT} && "
                f'claude{self._flags(ctx)} "$(cat {PROMPT_SENT})"'
            )
        flags = f" --model {ctx.model}" if ctx.model else ""
        return f"claude{flags} --continue || claude{flags}"

    def _flags(self, ctx: RenderContext) -> str:
        flags = ""
        if ctx.model:
            flags += f" --model {ctx.model}"
        if ctx.permission_mode:
            flags += f" --permission-mode {ctx.permission_mode}"
        if ctx.effort:
            flags += f" --effort {ctx.effort}"
        return flags

    def handoff_cmd(self, ctx: RenderContext, *, session: str, prompt: str) -> str:
        """Carry on the planning conversation on the implementation model.

        Resuming keeps everything the planner read while writing the plan, so
        the implementation does not pay to read it again. --model and
        --permission-mode override what a resumed session would restore.
        """
        return f"claude -r {shlex.quote(session)}{self._flags(ctx)} {shlex.quote(prompt)}"

    def hook_fragment(self) -> dict:
        """Machine-level hooks. Project settings files cannot carry hooks, and
        these must run outside the Bash sandbox to reach tmux anyway.

        ExitPlanMode fires once the human has accepted the plan, which is the
        one moment wtx can swap the model without losing anything. See
        orchestrate.py.
        """
        def entry(matcher: str, command: str) -> dict:
            return {
                "matcher": matcher,
                "hooks": [{"type": "command", "command": command}],
            }

        return {
            "Notification": [
                entry("permission_prompt", "wtx notify permission"),
                entry("idle_prompt", "wtx notify idle"),
            ],
            "Stop": [entry("*", "wtx notify stop")],
            "SessionStart": [entry("*", "wtx notify start")],
            "UserPromptSubmit": [entry("*", "wtx notify running")],
            "PostToolUse": [entry("ExitPlanMode", "wtx handoff")],
        }
