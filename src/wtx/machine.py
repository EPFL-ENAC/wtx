"""The machine-level pieces, the ones no repo can carry.

A project settings file cannot hold hooks, the shell needs a line in a startup
file, and tmux needs its own config. These are also exactly the files a
sandboxed agent cannot write, so this runs from a real terminal and prints
everything first.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .agents.base import get as get_agent
from .proc import say, warn

BASHRC_LINE = 'eval "$(wtx shell-init bash)"'


def picker_format() -> str:
    """The line choose-tree draws for one session.

    tmux reads the session option itself, so nothing runs per line and the
    picker stays instant. The colours come from notify so the picker, the
    board and the grid agree on what waiting looks like.
    """
    from .notify import TMUX_STYLE
    from .tmux import STATE_OPTION

    opt = f"#{{{STATE_OPTION}}}"
    style = ""
    for state, colour in TMUX_STYLE.items():
        style += f"#{{?#{{==:{opt},{state}}},#[{colour}],"
    style += "}" * len(TMUX_STYLE)
    return f"{style}#{{session_name}}#{{?{opt}, [{opt}],}}"


# -s, not -w: -w expands every session to its windows, so each session takes
# two lines and moving to the next one is two key presses. There is only ever
# one window per session here anyway.
TMUX_LINES = [
    "# wtx: which session is waiting for you",
    "set -g status-interval 5",
    """set -ag status-right ' #(wtx tmux-status)'""",
    f"bind s choose-tree -sZ -O name -F '{picker_format()}'",
    # One command, no `\;` sequence: in a config file tmux reads `\;` as a
    # literal argument, not as a separator, and a plain `;` would end the
    # binding and run the rest at load time. wtx monitor does the switch.
    # -b, or tmux shows whatever the command printed in copy mode, right on
    # top of the grid it just built.
    "bind g run-shell -b 'wtx monitor --grid'",
]

GIT_IGNORE_LINES = [
    "**/.claude/settings.local.json",
    "**/.claude/agents/Explore.md",
    "**/.claude/rules/wtx.md",
    "**/.claude/worktrees/",
    "**/.env.worktree",
    "**/.wt-logs/",
]

WT_CONFIG = """strategy = "custom"
pattern = "{.repo.Main}/.claude/worktrees/{.branch}"
separator = "-"
"""

EXPLORE_AGENT_USER = """---
name: Explore
description: Read-only search agent for broad fan-out searches. Use when answering means sweeping many files or directories and only the conclusion is needed.
model: haiku
tools: Bash, Glob, Grep, Read, NotebookRead, WebFetch, WebSearch, TodoWrite
---

You search the repository and report what you found. You never edit files.

Report file paths and line numbers, a short answer to the question asked, and
nothing else. Quote at most a few lines per file. If the answer is not there,
say so instead of guessing.
"""


# The skills shipped inside the package: /wtx-init sets a repo up, /wtx-create
# starts a worktree on a brief.
SKILL_NAMES = ("wtx-init", "wtx-create")


def skill_source(name: str) -> Path | None:
    """One skill shipped inside the package."""
    from importlib import resources

    try:
        base = resources.files("wtx") / "skills" / name
        path = Path(str(base))
        return path if path.is_dir() else None
    except (ModuleNotFoundError, TypeError):
        return None


def shipped_skills() -> list[str]:
    """The skills this install can actually copy."""
    return [name for name in SKILL_NAMES if skill_source(name) is not None]


def install_skill(target_root: Path) -> list[str]:
    """Copy the skills so /wtx-init and /wtx-create work in any repo.

    They live with wtx so they improve with the tool, not per repo.
    """
    done: list[str] = []
    for name in shipped_skills():
        src = skill_source(name)
        dst = target_root / name
        try:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        except OSError as exc:
            warn(f"could not install the {name} skill in {dst}: {exc}")
            continue
        done.append(name)
    return done


# Predecessors of wtx that may still be wired into the machine files. Their
# lines are commented out, not deleted, so nothing is lost.
OLD_BASHRC = ("scripts/wt-go.bash",)
OLD_NOTIFY = ("claude-notify",)


def _is_old_bashrc(line: str) -> bool:
    s = line.strip()
    return not s.startswith("#") and any(old in s for old in OLD_BASHRC)


def _is_wtx_hook(entry: dict) -> bool:
    """A hook entry wtx wrote. Everything it installs is a `wtx ...` command."""
    return any(
        isinstance(h, dict) and str(h.get("command", "")).startswith("wtx ")
        for h in entry.get("hooks", [])
    )


def _is_old_tmux(line: str) -> bool:
    """A picker or grid binding an older wtx, or a predecessor, wrote.

    Matched on what the binding does, not on its exact text: the picker line
    has changed twice already, and two `bind s` lines in the file means the
    last one read wins, which is not the one wtx just appended.
    """
    s = line.strip()
    if s.startswith("#") or s in TMUX_LINES:
        return False
    if s.startswith("bind s choose-tree"):
        return True
    return s.startswith("bind g ") and "wtx monitor" in s


@dataclass
class Change:
    path: Path
    what: str
    body: str
    kind: str = "append"  # append | create | merge-json
    stale: Callable[[str], bool] | None = None  # lines the body replaces

    def _text(self) -> str:
        try:
            return self.path.read_text()
        except OSError:
            return ""

    def stale_lines(self) -> list[str]:
        if self.stale is None or not self.path.is_file():
            return []
        return [ln for ln in self._text().splitlines() if self.stale(ln)]

    def needed(self) -> bool:
        if self.kind == "create":
            return not self.path.exists()
        if self.kind == "merge-json":
            return True
        if not self.path.is_file():
            return True
        return bool(self.missing_lines()) or bool(self.stale_lines())

    def missing_lines(self) -> list[str]:
        existing = self._text()
        return [
            line
            for line in self.body.splitlines()
            if line.strip() and not line.startswith("#") and line not in existing
        ]

    def comment_out_stale(self) -> int:
        """Replace each stale line with a commented copy. Returns the count."""
        if not self.stale_lines():
            return 0
        out = []
        count = 0
        for ln in self._text().splitlines():
            if self.stale(ln):  # type: ignore[misc]
                out.append(f"# replaced by wtx: {ln}")
                count += 1
            else:
                out.append(ln)
        self.path.write_text("\n".join(out) + "\n")
        return count


def _bashrc() -> Path:
    home = Path.home()
    for name in (".bashrc", ".bash_profile", ".profile"):
        p = home / name
        if p.is_file():
            return p
    return home / ".bashrc"


def planned(agent_tool: str = "claude") -> list[Change]:
    home = Path.home()
    changes = [
        Change(
            _bashrc(),
            "wtgo, wtdone and completion in every shell",
            f"\n# wtx\n{BASHRC_LINE}\n",
            stale=_is_old_bashrc,
        ),
        Change(
            home / ".tmux.conf",
            "show which session is waiting, in the status line and the picker",
            "\n" + "\n".join(TMUX_LINES) + "\n",
            stale=_is_old_tmux,
        ),
        Change(
            home / ".config" / "git" / "ignore",
            "keep per-worktree files out of every repo",
            "\n# wtx\n" + "\n".join(GIT_IGNORE_LINES) + "\n",
        ),
        Change(
            home / ".config" / "wt" / "config.toml",
            "put worktrees under <repo>/.claude/worktrees/<branch>",
            WT_CONFIG,
            kind="create",
        ),
    ]
    if agent_tool == "claude":
        changes.append(
            Change(
                home / ".claude" / "agents" / "Explore.md",
                "search on a small model instead of inheriting the main one",
                EXPLORE_AGENT_USER,
                kind="create",
            )
        )
        changes.append(
            Change(
                home / ".claude" / "settings.json",
                "notify and handoff hooks: which session is waiting, and which "
                "model implements an accepted plan",
                json.dumps({"hooks": get_agent("claude").hook_fragment()}, indent=2),
                kind="merge-json",
            )
        )
    return changes


def _merge_hooks(path: Path, fragment: dict) -> bool:
    """Add wtx's hooks to the user's settings without touching the rest."""
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            warn(f"{path} is not readable JSON ({exc}), not touching it")
            return False
    hooks = data.setdefault("hooks", {})
    changed = False
    for event, entries in fragment.items():
        current = hooks.setdefault(event, [])
        for entry in entries:
            # First drop the old claude-notify hook on this matcher, even when
            # ours is already there: an earlier run may have added ours next
            # to it, and then every event notified twice. Keep anyone else's.
            before = len(current)
            current[:] = [
                e
                for e in current
                if not (
                    e.get("matcher") == entry.get("matcher")
                    and any(old in json.dumps(e.get("hooks", [])) for old in OLD_NOTIFY)
                )
            ]
            changed = changed or len(current) != before
            same = [
                e
                for e in current
                if e.get("matcher") == entry.get("matcher")
                and json.dumps(e.get("hooks")) == json.dumps(entry.get("hooks"))
            ]
            if same:
                continue
            # Replace an older wtx hook on the same matcher.
            current[:] = [
                e
                for e in current
                if not (e.get("matcher") == entry.get("matcher") and _is_wtx_hook(e))
            ]
            current.append(entry)
            changed = True
    if not changed:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(path.suffix + ".wtx-backup")
    if path.is_file():
        backup.write_text(path.read_text())
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def show(agent_tool: str = "claude") -> str:
    lines = [
        "wtx needs these machine-level pieces. Run `wtx install-machine --apply`",
        "from a real terminal, or make the changes by hand.",
        "",
    ]
    for change in planned(agent_tool):
        status = "needed" if change.needed() else "already there"
        lines.append(f"--- {change.path}  ({status}): {change.what}")
        for old in change.stale_lines():
            lines.append(f"    replaces (commented out, not deleted): {old.strip()}")
        lines.append(change.body.strip())
        lines.append("")
    if agent_tool == "claude":
        for name in shipped_skills():
            lines.append(f"--- {Path.home() / '.claude' / 'skills' / name}: the /{name} skill")
            lines.append("")
    lines.append(
        "A coding agent cannot write these files, and should not: they decide what "
        "it is allowed to do."
    )
    return "\n".join(lines)


def apply(agent_tool: str = "claude") -> int:
    applied = 0
    for change in planned(agent_tool):
        if change.kind == "merge-json":
            fragment = json.loads(change.body)["hooks"]
            try:
                if _merge_hooks(change.path, fragment):
                    say(f"updated {change.path}")
                    applied += 1
                else:
                    say(f"already set: {change.path}")
            except OSError as exc:
                warn(f"could not write {change.path}: {exc}")
            continue
        if not change.needed():
            say(f"already set: {change.path}")
            continue
        try:
            change.path.parent.mkdir(parents=True, exist_ok=True)
            if change.kind == "create":
                change.path.write_text(change.body)
            else:
                if change.path.is_file():
                    backup = change.path.with_suffix(change.path.suffix + ".wtx-backup")
                    backup.write_text(change.path.read_text())
                replaced = change.comment_out_stale()
                if replaced:
                    say(f"commented out {replaced} old line(s) in {change.path}")
                if change.missing_lines():
                    with change.path.open("a") as fh:
                        fh.write(change.body)
            say(f"updated {change.path}")
            applied += 1
        except OSError as exc:
            warn(f"could not write {change.path}: {exc}")
    if agent_tool == "claude":
        skills = Path.home() / ".claude" / "skills"
        try:
            skills.mkdir(parents=True, exist_ok=True)
            for name in install_skill(skills):
                say(f"installed the /{name} skill in {skills}")
                applied += 1
        except OSError as exc:
            warn(f"could not create {skills}: {exc}")
    if applied:
        say("open a new shell, and run `tmux source-file ~/.tmux.conf` in tmux")
    return applied
