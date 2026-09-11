"""Test fixtures: a real temp git repo, and fake binaries that record calls.

The real git is used because the traps this tool exists for are git behaviours.
wt, tmux and the agents are faked: a test must never touch the developer's tmux
server, and must never push anywhere real. The fake wt does what the real one
does with plain git, minus the .wt.toml hooks.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--run-tmux",
        action="store_true",
        default=False,
        help="run the tests that need a real tmux server",
    )


def pytest_collection_modifyitems(config, items) -> None:
    if config.getoption("--run-tmux"):
        return
    skip = pytest.mark.skip(reason="needs a real tmux server, pass --run-tmux")
    for item in items:
        if "tmux" in item.keywords:
            item.add_marker(skip)


FAKE = """#!/bin/sh
printf '%s\\t%s\\n' "{name}" "$*" >> "$WTX_TEST_CALLS"
{body}
exit 0
"""

BODIES = {
    "tmux": """case "$1" in
  has-session) if [ -f "$WTX_TEST_SESSIONS" ] && grep -qxF "${3#=}" "$WTX_TEST_SESSIONS"; then exit 0; else exit 1; fi;;
  new-session) echo "${4}" >> "$WTX_TEST_SESSIONS";;
  kill-session) if [ -f "$WTX_TEST_SESSIONS" ]; then grep -vxF "${3#=}" "$WTX_TEST_SESSIONS" > "$WTX_TEST_SESSIONS.tmp" || true; mv "$WTX_TEST_SESSIONS.tmp" "$WTX_TEST_SESSIONS"; fi;;
  display-message) echo '%0';;
  split-window) echo "%$$";;
  list-sessions) [ -f "$WTX_TEST_SESSIONS" ] && cat "$WTX_TEST_SESSIONS";;
  list-panes) echo '%0 agent';;
  show-options) echo '';;
esac""",
    "wt": """slug=$(printf '%s' "$4" | tr / -)
case "$3" in
  create) git worktree add -q -b "$4" ".claude/worktrees/$slug" "$5";;
  checkout) git worktree add -q ".claude/worktrees/$slug" "$4";;
  remove) p=$(git worktree list --porcelain | awk -v b="refs/heads/$4" '/^worktree /{w=$2} $0=="branch "b{print w}')
          [ -n "$p" ] && git worktree remove --force "$p";;
esac""",
    "npm": 'case "$*" in *ci*) mkdir -p node_modules;; esac',
    "pnpm": 'case "$*" in *install*) mkdir -p node_modules;; esac',
    "uv": 'case "$1" in sync) mkdir -p .venv;; esac',
    "npx": "",
    "curl": "",
    "claude": "",
    "opencode": "",
    "notify-send": 'for a in "$@"; do if [ "$a" = "-p" ]; then echo 7; break; fi; done',
    # `gdbus monitor` blocks for real. The stub ends straight away, which is
    # what a notification closed by something other than a click looks like.
    "gdbus": 'case "$1" in monitor) exit 0;; esac',
    "fuser": "",
    "ss": "",
    "gh": 'case "$*" in *"pr view"*) exit 1;; esac',
}


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory of recording stubs, put first on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    sessions = tmp_path / "sessions.txt"
    calls.write_text("")
    sessions.write_text("")
    for name, body in BODIES.items():
        f = bindir / name
        f.write_text(FAKE.format(name=name, body=body))
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("WTX_TEST_CALLS", str(calls))
    monkeypatch.setenv("WTX_TEST_SESSIONS", str(sessions))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.delenv("TMUX", raising=False)
    return bindir


def calls_of(bindir: Path, name: str) -> list[str]:
    log = Path(os.environ["WTX_TEST_CALLS"])
    if not log.is_file():
        return []
    out = []
    for line in log.read_text().splitlines():
        who, _, args = line.partition("\t")
        if who == name:
            out.append(args)
    return out


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo shaped like the real ones: backend, frontend, a remote, dev base."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    root = tmp_path / "app"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "dev", str(root)], check=True)
    _git(["config", "user.email", "t@example.org"], root)
    _git(["config", "user.name", "Test"], root)
    (root / "backend").mkdir()
    (root / "frontend").mkdir()
    (root / ".python-version").write_text("3.12.7\n")
    (root / "Makefile").write_text("lint:\n\ttrue\ntest:\n\ttrue\n")
    (root / "backend" / "Makefile").write_text("BACKEND_PORT ?= 8000\nrun:\n\ttrue\n")
    (root / "backend" / "pyproject.toml").write_text('[project]\nname="b"\n')
    (root / "backend" / "uv.lock").write_text("")
    (root / "package.json").write_text('{"name":"app","scripts":{"lint":"true"}}')
    (root / "package-lock.json").write_text("{}")
    (root / "frontend" / "package.json").write_text('{"name":"f","scripts":{"dev":"true"}}')
    (root / "frontend" / "package-lock.json").write_text("{}")
    (root / "frontend" / "vite.config.ts").write_text("export default { server: { port: 5173 } }\n")
    (root / ".gitignore").write_text("node_modules/\n.venv/\n")
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "init"], root)
    _git(["remote", "add", "origin", str(remote)], root)
    _git(["push", "-q", "-u", "origin", "dev"], root)
    return root


@pytest.fixture
def sibling(tmp_path: Path) -> Path:
    """A sibling library repo, the thing a worktree pairs with."""
    remote = tmp_path / "model-remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    root = tmp_path / "model"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    _git(["config", "user.email", "t@example.org"], root)
    _git(["config", "user.name", "Test"], root)
    (root / "pyproject.toml").write_text('[project]\nname="model"\nversion="1"\n')
    (root / "model").mkdir()
    (root / "model" / "__init__.py").write_text("")
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "init"], root)
    _git(["remote", "add", "origin", str(remote)], root)
    _git(["push", "-q", "-u", "origin", "main"], root)
    return root


@pytest.fixture
def wtx_repo(repo: Path) -> Path:
    """The app repo with wtx.toml written and committed."""
    from wtx import init as init_mod

    answers = init_mod.scan(repo)
    answers.pop("_notes", None)
    init_mod.write_all(repo, answers)
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "chore: wtx"], repo)
    _git(["push", "-q", "origin", "dev"], repo)
    return repo


@pytest.fixture(autouse=True)
def _no_dry_run():
    from wtx import proc

    proc.set_dry_run(False)
    yield
    proc.set_dry_run(False)
