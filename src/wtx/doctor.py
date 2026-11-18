"""Check the machine has what wtx needs, and say how to fix what it does not."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import git, guard, tmux, workmux
from .config import CONFIG_NAME
from .proc import capture, capture_code, which


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    hard: bool = True


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
            "checks": [asdict(c) for c in self.checks],
        }


# --locked, or cargo fails on a missing `nix` feature.
WORKMUX_INSTALL = (
    "cargo install workmux --locked, or brew install raine/workmux/workmux. "
    "See https://github.com/raine/workmux"
)
WORKMUX_HOOKS = f"{' '.join(workmux.SETUP_HOOKS)}, from a real terminal (it asks a question)"


def _workmux_merge_skill() -> Path | None:
    """workmux's merge skill merges locally and removes the worktree: an agent
    that runs it goes around `wtx land`, its checks and its plans."""
    skill = Path("~/.claude/skills/merge/SKILL.md").expanduser()
    try:
        return skill.parent if "workmux" in skill.read_text() else None
    except OSError:
        return None


def _binary(name: str, fix: str, *, hard: bool = True) -> Check:
    path = which(name)
    return Check(name, bool(path), path or "not on PATH", fix, hard=hard)


def run(cwd: Path | None = None) -> Report:
    here = (cwd or Path.cwd()).resolve()
    checks: list[Check] = []

    checks.append(_binary("git", "install git"))
    checks.append(_binary("workmux", WORKMUX_INSTALL))
    checks.append(_binary("tmux", "install tmux"))
    if which("tmux"):
        tmux_state = tmux.server_status()
        checks.append(
            Check(
                "tmux server reachable",
                tmux_state[0] in ("up", "absent"),
                "",
                tmux.server_remedy(*tmux_state),
                hard=False,
            )
        )
    checks.append(
        _binary(
            "gh", "install the GitHub CLI and run gh auth login, needed by wtx land", hard=False
        )
    )
    checks.append(
        _binary("ss", "install iproute2, without it wtx cannot see used ports", hard=False)
    )
    checks.append(
        _binary("notify-send", "install libnotify-bin for desktop notifications", hard=False)
    )
    checks.append(
        _binary(
            "hunk",
            "curl -fsSL https://hunk.dev/install.sh | sh, for wtx review. "
            "Without it the review is printed as text",
            hard=False,
        )
    )
    checks.append(
        _binary(
            "gdbus",
            "install libglib2.0-bin, without it a clicked notification cannot "
            "bring the terminal to the front",
            hard=False,
        )
    )

    if which("gh"):
        # The exit code, not the text: older gh prints the whole status to
        # stderr and a check that reads stdout then always says no.
        ok, _, _ = capture_code(["gh", "auth", "status"])
        checks.append(Check("gh authenticated", ok == 0, "", "gh auth login", hard=False))

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
    text = ""
    if settings.is_file():
        try:
            text = settings.read_text()
        except OSError:
            text = ""
    checks.append(
        Check(
            "agent notify hooks",
            "wtx notify" in text,
            str(settings),
            "wtx install-machine --apply (project settings cannot carry hooks)",
            hard=False,
        )
    )
    checks.append(
        Check(
            "agent handoff hook",
            "wtx handoff" in text,
            str(settings),
            "wtx install-machine --apply ([agent.orchestration] does nothing without it)",
            hard=False,
        )
    )
    if which("workmux"):
        checks.append(
            Check(
                "workmux status hooks",
                workmux.hooks_installed(text),
                str(settings),
                WORKMUX_HOOKS,
                hard=False,
            )
        )
    merge_skill = _workmux_merge_skill()
    checks.append(
        Check(
            "no workmux merge skill",
            merge_skill is None,
            str(merge_skill or ""),
            f"rm -r {merge_skill}: it merges and removes the worktree around wtx land",
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
        checks.append(
            Check(
                "repo checks",
                False,
                f"{here} is not in a git repo, skipped",
                "run wtx doctor from inside a repo to check its wtx.toml and guard",
                hard=False,
            )
        )
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
    checks.append(
        Check(
            "no .wt.toml left",
            not wt_toml.is_file(),
            str(wt_toml),
            "wtx init removes it: workmux runs the hooks now, from the config wtx writes",
            hard=False,
        )
    )
    generated = workmux.config_path(main)
    try:
        foreign = generated.is_file() and not generated.read_text().startswith(workmux.MARKER)
    except OSError:
        foreign = False
    checks.append(
        Check(
            f"{workmux.CONFIG_NAME} is the one wtx writes",
            not foreign,
            str(generated),
            "move what you need into wtx.toml, then delete it: wtx go writes it again",
        )
    )
    # A config on a branch never reaches what wtx runs (it passes its own with
    # --config, and runs remove from the main checkout), but a workmux command
    # typed in that worktree reads it.
    own = [
        str(item.path)
        for item in git.list_worktrees(main)
        if item.path.resolve() != main.resolve() and (item.path / workmux.CONFIG_NAME).is_file()
    ]
    checks.append(
        Check(
            f"no worktree with its own {workmux.CONFIG_NAME}",
            not own,
            ", ".join(own),
            "remove it from the branch: a workmux command typed in that worktree reads it",
            hard=False,
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
            # lefthook runs pre-push.old: a guard copy there makes a push loop.
            stale = guard.stale_copies(guard.hooks_dir(main))
            checks.append(
                Check(
                    "no stale push guard copy",
                    not stale,
                    ", ".join(p.name for p in stale),
                    "wtx setup, from a real terminal (it removes them)",
                    hard=False,
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
