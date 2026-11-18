"""Test fixtures: a real temp git repo, and fake binaries that record calls.

The real git is used because the traps this tool exists for are git behaviours.
workmux, tmux and the agents are faked: a test must never touch the developer's
tmux server, and must never push anywhere real. The fake workmux does what the
real one does with plain git, minus the hooks: they would run whatever `wtx`
is installed on the machine, not the code under test.
"""

from __future__ import annotations

import os
import shutil
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
    # Goes in step 4, when nothing calls wt any more.
    "wt": """slug=$(printf '%s' "$4" | tr / -)
case "$3" in
  create) git worktree add -q -b "$4" ".claude/worktrees/$slug" "$5";;
  checkout) git worktree add -q ".claude/worktrees/$slug" "$4";;
  remove) p=$(git worktree list --porcelain | awk -v b="refs/heads/$4" '/^worktree /{w=$2} $0=="branch "b{print w}')
          [ -n "$p" ] && git worktree remove $5 "$p";;
esac""",
    "npm": 'case "$*" in *ci*) mkdir -p node_modules;; esac',
    "pnpm": 'case "$*" in *install*) mkdir -p node_modules;; esac',
    "uv": 'case "$1" in sync) mkdir -p .venv;; esac',
    "npx": "",
    "curl": "",
    "claude": "",
    "opencode": "",
    "hunk": "",
    "notify-send": 'for a in "$@"; do if [ "$a" = "-p" ]; then echo 7; break; fi; done',
    # `gdbus monitor` blocks for real. The stub ends straight away, which is
    # what a notification closed by something other than a click looks like.
    "gdbus": 'case "$1" in monitor) exit 0;; esac',
    "fuser": "",
    "ss": "",
    "gh": 'case "$*" in *"pr view"*) exit 1;; esac',
}


# Python, not sh: it reads the JSON config wtx writes. Same call log as the
# others. Sessions go in the same file the fake tmux reads.
FAKE_WORKMUX = r"""
import json, os, subprocess, sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["WTX_TEST_CALLS"], "a") as log:
    log.write("workmux\t" + " ".join(args) + "\n")


def git(*a):
    p = subprocess.run(["git", *a], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        sys.exit(1)
    return p.stdout.strip()


def opt(name):
    if name in args:
        i = args.index(name)
        value = args[i + 1]
        del args[i : i + 2]
        return value
    return None


def flag(*names):
    found = False
    for name in names:
        while name in args:
            args.remove(name)
            found = True
    return found


def load(path):
    path = Path(path)
    if not path.is_file():
        return {}
    text = "\n".join(l for l in path.read_text().splitlines() if not l.startswith("#"))
    return json.loads(text)


def sessions(change):
    f = Path(os.environ["WTX_TEST_SESSIONS"])
    names = [n for n in f.read_text().splitlines() if n] if f.is_file() else []
    f.write_text("".join(n + "\n" for n in change(names)))


main = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir")).parent
meta = lambda name, key: f"workmux.worktree.{name}.{key}"
cmd = args.pop(0) if args else ""
if cmd == "--version":
    print("workmux 0.1.282")
elif cmd == "add":
    cfg = load(opt("--config"))
    name, base = opt("--name"), opt("--base")
    flag("--headless", "--background", "-b")
    branch = args[-1]
    path = main / cfg.get("worktree_dir", "..") / name
    known = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "refs/heads/" + branch],
                           capture_output=True).returncode == 0
    if known:
        git("worktree", "add", "-q", str(path), branch)
    else:
        git("worktree", "add", "-q", "-b", branch, str(path), base or "HEAD")
    git("config", "--local", meta(name, "attachment"), "headless")
elif cmd == "open":
    cfg = load(opt("--config"))
    target = opt("--target-name")
    flag("--background", "-b")
    name = args[-1]
    session = cfg.get("window_prefix", "wm-") + (target or name)
    sessions(lambda names: names if session in names else names + [session])
    git("config", "--local", meta(name, "mode"), "session")
    git("config", "--local", meta(name, "attachment"), "multiplexer")
    if target:
        git("config", "--local", meta(name, "target-session"), target)
elif cmd in ("remove", "close"):
    # No --config here, like the real one: the config is what it finds from
    # its cwd. wtx runs it from the main checkout.
    cfg = load(Path.cwd() / ".workmux.yaml")
    force = flag("--force", "-f")
    flag("--keep-branch", "-k")
    name = args[-1]
    target = subprocess.run(["git", "config", "--local", "--get", meta(name, "target-session")],
                            capture_output=True, text=True).stdout.strip() or name
    session = cfg.get("window_prefix", "wm-") + target
    if cmd == "remove":
        path = next((p for p in (l[len("worktree "):] for l in git(
            "worktree", "list", "--porcelain").splitlines() if l.startswith("worktree "))
            if Path(p).name == name), None)
        if path is None:
            sys.stderr.write(f"Worktree '{name}' not found.\n")
            sys.exit(1)
        git("worktree", "remove", *(["--force"] if force else []), path)
        subprocess.run(["git", "config", "--local", "--remove-section", f"workmux.worktree.{name}"],
                       capture_output=True)
    sessions(lambda names: [n for n in names if n != session])
"""


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(cwd: Path, message: str = "work") -> None:
    """An empty commit, so a branch is ahead of its base."""
    _git(["commit", "-q", "--allow-empty", "-m", message], cwd)


def fake_sessions(*names: str) -> None:
    """Tell the fake tmux which sessions exist."""
    Path(os.environ["WTX_TEST_SESSIONS"]).write_text("".join(f"{n}\n" for n in names))


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
    workmux = bindir / "workmux"
    workmux.write_text(f"#!{sys.executable}\n{FAKE_WORKMUX}")
    workmux.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("WTX_TEST_CALLS", str(calls))
    monkeypatch.setenv("WTX_TEST_SESSIONS", str(sessions))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    # wtx looks for hunk in ~/.hunk/bin too. A test must not find the real one.
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
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


# The git commands that talk to a remote.
REMOTE_GIT = ("fetch", "push", "pull", "ls-remote")


def remote_git_calls(bindir: Path) -> list[str]:
    """What the git_calls fixture saw going to a remote. A failed fetch against
    a dead remote leaves no other trace: wtx ignores its exit code."""
    return [c for c in calls_of(bindir, "git") if c.split(" ", 1)[0] in REMOTE_GIT]


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


@pytest.fixture
def git_calls(wtx_repo: Path, fake_bin: Path) -> Path:
    """Log every git call, then run the real git.

    After wtx_repo, on purpose: that fixture pushes, and the log must hold only
    what the test did.
    """
    real = shutil.which("git")
    wrapper = fake_bin / "git"
    wrapper.write_text(
        f"""#!/bin/sh
printf '%s\\t%s\\n' git "$*" >> "$WTX_TEST_CALLS"
exec {real} "$@"
"""
    )
    wrapper.chmod(0o755)
    return fake_bin


@pytest.fixture
def local_repo(wtx_repo: Path, git_calls: Path) -> Path:
    """A repo that lands by local merge, with no origin/dev and a dead remote."""
    cfg = wtx_repo / "wtx.toml"
    cfg.write_text(cfg.read_text().replace("[repo]\n", '[repo]\nland = "merge"\n', 1))
    _git(["commit", "-qam", "land by local merge"], wtx_repo)
    _git(["remote", "set-url", "origin", str(wtx_repo.parent / "gone.git")], wtx_repo)
    _git(["branch", "--unset-upstream", "dev"], wtx_repo)
    _git(["update-ref", "-d", "refs/remotes/origin/dev"], wtx_repo)
    return wtx_repo


@pytest.fixture(autouse=True)
def _no_dry_run():
    from wtx import proc

    proc.set_dry_run(False)
    yield
    proc.set_dry_run(False)


@pytest.fixture(autouse=True)
def no_fork(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """capture() fires the handoff itself. No test may fork the real process
    that respawns a pane, so keep the records it would have run instead.

    autouse and in conftest, not in one module: a test that reaches capture()
    by accident must not start forking either.
    """
    from wtx import orchestrate

    fired: list[Path] = []
    monkeypatch.setattr(orchestrate, "_spawn", lambda record, **kw: fired.append(record) or True)
    return fired
