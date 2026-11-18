"""Tests that need a real tmux server.

Skipped unless you ask for them, and they cannot run inside an agent sandbox,
which has no access to the tmux socket:

    python -m pytest tests/ -m tmux --run-tmux

They use their own tmux socket, never the one you are working in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from wtx import context
from wtx import setup as setup_mod
from wtx.agents import base as agents

pytestmark = pytest.mark.tmux

SOCKET = "wtx-tests"
# Found before fake_bin puts its fake tmux first on PATH. Looked up at call
# time, "tmux" was the fake: the probe below never saw a server and every
# test here was skipped, even with --run-tmux.
REAL_TMUX = shutil.which("tmux") or "tmux"
# Same for workmux: fake_bin puts a fake one first.
REAL_WORKMUX = shutil.which("workmux")


def tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [REAL_TMUX, "-L", SOCKET, *args], capture_output=True, text=True, check=False
    )


@pytest.fixture
def real_tmux(monkeypatch: pytest.MonkeyPatch, fake_bin: Path):
    """A private tmux server, on a socket of its own.

    fake_bin is named so it is set up first: this reads WTX_TEST_CALLS, which
    that fixture sets, and relying on the test's argument order to get it was a
    trap waiting to spring.
    """
    if shutil.which(REAL_TMUX) is None:
        pytest.skip("no tmux")
    # "no server running" and "cannot reach the socket" look alike from the
    # outside, so prove a server can actually start before promising to test one.
    probe = tmux("new-session", "-d", "-s", "wtx-probe")
    listed = tmux("list-sessions", "-F", "#{session_name}")
    if "wtx-probe" not in listed.stdout.split():
        tmux("kill-server")
        why = (probe.stderr or probe.stdout or listed.stderr).strip() or "unknown"
        pytest.skip(f"no usable tmux server here: {why}")
    tmux("kill-session", "-t", "=wtx-probe")
    # Every tmux call in wtx goes through the binary, so a wrapper on PATH is
    # enough to keep these tests off the developer's own server.
    bindir = Path(os.environ["WTX_TEST_CALLS"]).parent / "realbin"
    bindir.mkdir(exist_ok=True)
    wrapper = bindir / "tmux"
    wrapper.write_text(f'#!/bin/sh\nexec {REAL_TMUX} -L {SOCKET} "$@"\n')
    wrapper.chmod(0o755)
    # The panes run `wtx pane`: it must be this code, not the wtx installed on
    # the machine.
    src = Path(__file__).resolve().parents[1] / "src"
    wtx = bindir / "wtx"
    wtx.write_text(f'#!/bin/sh\nPYTHONPATH={src} exec {sys.executable} -m wtx "$@"\n')
    wtx.chmod(0o755)
    if REAL_WORKMUX:
        real = bindir / "workmux"
        real.write_text(f'#!/bin/sh\nexec {REAL_WORKMUX} "$@"\n')
        real.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("TMUX", raising=False)
    yield
    tmux("kill-server")


def test_a_real_session_gets_its_panes_and_roles(wtx_repo: Path, fake_bin: Path, real_tmux) -> None:
    """workmux builds the session, then each pane tags itself with `wtx pane`."""
    from wtx import tmux as tmux_mod

    if REAL_WORKMUX is None:
        pytest.skip("no workmux")

    path = wtx_repo / ".claude" / "worktrees" / "feat-real"
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feat/real", str(path), "origin/dev"],
        cwd=wtx_repo,
        check=True,
        capture_output=True,
    )
    ctx = context.load(root=path)
    setup_mod.run_setup(ctx, start_tmux=False)
    ctx.reload_env()
    tmux_mod.ensure_session(ctx, agents.get("claude"), [], attach_after=False)

    assert tmux_mod.has_session(ctx.session)
    # The panes tag themselves when their shell runs the line workmux typed.
    roles: list[str] = []
    for _ in range(100):
        roles = tmux("list-panes", "-t", f"={ctx.session}", "-F", "#{@wt_role}").stdout.split()
        if len(roles) == len(ctx.cfg.panes.panes):
            break
        time.sleep(0.1)
    assert roles.count("agent") == 1
    assert len(roles) == len(ctx.cfg.panes.panes)
    assert tmux_mod.agent_pane_id(ctx.session)

    tmux_mod.set_session_option(ctx.session, tmux_mod.STATE_OPTION, "permission")
    # set-option and show-options take a pane: "=name" alone is no session
    # to them, "=name:" is.
    shown = tmux("show-options", "-v", "-t", f"={ctx.session}:", tmux_mod.STATE_OPTION)
    assert shown.stdout.strip() == "permission"

    tmux_mod.kill_session(ctx.session)
    assert not tmux_mod.has_session(ctx.session)


def test_a_real_tmux_takes_the_land_result_message(fake_bin: Path, real_tmux) -> None:
    """The end of an in-session land: a message that stays until a key, on a
    named client. A client that left in the meantime must not be an error, the
    clean up job has nobody to tell."""
    tmux("new-session", "-d", "-s", "wtx-land")
    client = tmux("display-message", "-p", "#{client_name}")
    assert client.returncode == 0

    shown = tmux("display-message", "-d", "0", "-c", "/dev/pts/999", "wtx: landed")
    assert shown.returncode == 0, shown.stderr
    assert not shown.stderr
