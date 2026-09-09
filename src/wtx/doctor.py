"""Check the machine has what wtx needs, and say how to fix what it does not."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import git, guard
from .config import CONFIG_NAME
from .proc import capture, which


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    hard: bool = True

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "fix": self.fix,
            "hard": self.hard,
        }


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.hard]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.hard]

    def as_dict(self) -> dict:
        return {
            "ok": not self.failed,
            "checks": [c.as_dict() for c in self.checks],
        }


def _binary(name: str, fix: str, *, hard: bool = True) -> Check:
    path = which(name)
    return Check(name, bool(path), path or "not on PATH", fix, hard=hard)


def run(cwd: Path | None = None) -> Report:
    here = (cwd or Path.cwd()).resolve()
    checks: list[Check] = []

    checks.append(_binary("git", "install git"))
    checks.append(
        _binary("wt", "go install github.com/timvw/wt@latest, then wt init")
    )
    checks.append(_binary("tmux", "install tmux"))
    checks.append(
        _binary("gh", "install the GitHub CLI and run gh auth login, needed by wtx land", hard=False)
    )
    checks.append(
        _binary("ss", "install iproute2, without it wtx cannot see used ports", hard=False)
    )
    checks.append(
        _binary("notify-send", "install libnotify-bin for desktop notifications", hard=False)
    )

    if which("wt"):
        cfg_file = Path("~/.config/wt/config.toml").expanduser()
        raw = cfg_file.read_text() if cfg_file.is_file() else ""
        pattern = ""
        for line in raw.splitlines():
            if line.strip().startswith("pattern"):
                pattern = line.split("=", 1)[-1].strip().strip('"')
        checks.append(
            Check(
                "wt worktree pattern",
                ".claude/worktrees" in pattern,
                pattern or "no ~/.config/wt/config.toml",
                'set pattern = "{.repo.Main}/.claude/worktrees/{.branch}" in ~/.config/wt/config.toml',
                hard=False,
            )
        )

    if which("gh"):
        code = capture(["gh", "auth", "status"])
        checks.append(
            Check(
                "gh authenticated",
                "Logged in" in code or "logged in" in code,
                "",
                "gh auth login",
                hard=False,
            )
        )

    agents = [name for name in ("claude", "opencode") if which(name)]
    checks.append(
        Check(
            "a coding agent",
            bool(agents),
            ", ".join(agents) or "neither claude nor opencode found",
            "install Claude Code or opencode",
        )
    )

    tmux_conf = Path("~/.tmux.conf").expanduser()
    conf_text = tmux_conf.read_text() if tmux_conf.is_file() else ""
    checks.append(
        Check(
            "tmux status shows waiting sessions",
            "wtx tmux-status" in conf_text,
            str(tmux_conf),
            "wtx install-machine --apply",
            hard=False,
        )
    )

    settings = Path("~/.claude/settings.json").expanduser()
    hooks_ok = False
    if settings.is_file():
        try:
            hooks_ok = "wtx notify" in settings.read_text()
        except OSError:
            hooks_ok = False
    checks.append(
        Check(
            "agent notify hooks",
            hooks_ok,
            str(settings),
            "wtx install-machine --apply (project settings cannot carry hooks)",
            hard=False,
        )
    )

    merge = capture(["git", "config", "--global", "branch.autoSetupMerge"])
    checks.append(
        Check(
            "git branch.autoSetupMerge",
            merge == "simple",
            merge or "unset",
            "git config --global branch.autoSetupMerge simple",
            hard=False,
        )
    )

    main = git.main_checkout(here)
    if main is None:
        checks.append(Check("inside a git repo", False, str(here), "cd into a repo"))
        return Report(checks)

    cfg_path = main / CONFIG_NAME
    checks.append(
        Check(
            f"{CONFIG_NAME} in {main.name}",
            cfg_path.is_file(),
            str(cfg_path),
            "wtx init",
        )
    )
    wt_toml = main / ".wt.toml"
    calls_wtx = wt_toml.is_file() and "wtx hook" in wt_toml.read_text()
    checks.append(
        Check(
            ".wt.toml calls wtx",
            calls_wtx,
            str(wt_toml),
            "wtx init, or add the three hook lines by hand",
        )
    )

    if cfg_path.is_file():
        from . import config as config_mod

        try:
            cfg = config_mod.load(cfg_path)
            problems = config_mod.validate(cfg)
            checks.append(
                Check(
                    "config is valid",
                    not problems,
                    "; ".join(problems),
                    "fix wtx.toml",
                )
            )
            checks.append(
                Check(
                    "push guard installed",
                    guard.is_installed(main),
                    str(guard.hooks_dir(main) / "pre-push"),
                    "wtx setup, from a real terminal",
                )
            )
        except config_mod.ConfigError as exc:
            checks.append(Check("config is valid", False, str(exc), "fix wtx.toml"))

    if os.environ.get("CLAUDE_CODE") or os.environ.get("CLAUDECODE"):
        checks.append(
            Check(
                "running outside an agent sandbox",
                False,
                "wtx is running inside a coding agent",
                "run wtx setup, wtx go and wtx install-machine from a real terminal: "
                "a sandboxed agent cannot write .git/hooks or reach the tmux socket",
                hard=False,
            )
        )
    return Report(checks)


def render(report: Report) -> str:
    lines = []
    for c in report.checks:
        mark = "ok  " if c.ok else ("FAIL" if c.hard else "warn")
        line = f"{mark}  {c.name}"
        if c.detail:
            line += f"  ({c.detail})"
        lines.append(line)
        if not c.ok and c.fix:
            lines.append(f"        fix: {c.fix}")
    if report.failed:
        lines.append("")
        lines.append(f"{len(report.failed)} check(s) failed.")
    return "\n".join(lines)


def as_json(report: Report) -> str:
    return json.dumps(report.as_dict(), indent=2)
