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
from pathlib import Path

import pytest
from wtx import context, setup as setup_mod
from wtx.agents import base as agents

pytestmark = pytest.mark.tmux

SOCKET = "wtx-tests"


def tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-L", SOCKET, *args], capture_output=True, text=True, check=False
    )


@pytest.fixture
def real_tmux(monkeypatch: pytest.MonkeyPatch):
    if shutil.which("tmux") is None:
        pytest.skip("no tmux")
    # "no server running" and "cannot reach the socket" look alike from the
    # outside, so prove a server can actually start before promising to test one.
    probe = tmux("new-session", "-d", "-s", "wtx-probe")
    if probe.returncode != 0 or tmux("has-session", "-t", "=wtx-probe").returncode != 0:
        tmux("kill-server")
        pytest.skip(f"no usable tmux server here: {probe.stderr.strip() or 'unknown'}")
    tmux("kill-session", "-t", "=wtx-probe")
    # Every tmux call in wtx goes through the binary, so a wrapper on PATH is
    # enough to keep these tests off the developer's own server.
    bindir = Path(os.environ["WTX_TEST_CALLS"]).parent / "realbin"
    bindir.mkdir(exist_ok=True)
    wrapper = bindir / "tmux"
    wrapper.write_text(f'#!/bin/sh\nexec {shutil.which("tmux")} -L {SOCKET} "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("TMUX", raising=False)
    yield
    tmux("kill-server")


def test_a_real_session_gets_its_panes_and_roles(
    wtx_repo: Path, fake_bin: Path, real_tmux
) -> None:
    from wtx import tmux as tmux_mod

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
    roles = tmux(
        "list-panes", "-t", f"={ctx.session}", "-F", "#{@wt_role}"
    ).stdout.split()
    assert roles.count("agent") == 1
    assert len(roles) == len(ctx.cfg.panes.panes)
    assert tmux_mod.agent_pane_id(ctx.session)

    tmux_mod.set_session_option(ctx.session, tmux_mod.STATE_OPTION, "permission")
    assert tmux_mod.get_session_option(ctx.session, tmux_mod.STATE_OPTION) == "permission"

    tmux_mod.kill_session(ctx.session)
    assert not tmux_mod.has_session(ctx.session)
