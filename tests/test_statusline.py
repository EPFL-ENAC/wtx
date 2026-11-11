"""The Claude Code status line, and the phase wtx records for it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import commit
from test_integration import _accept, _enable_orchestration, make_worktree, run

from wtx import context, notify, statusline
from wtx import setup as setup_mod

# -- the auto-compact limit ----------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write the user and project settings files. Returns the project dir."""
    home = tmp_path / "claude-home"
    project = tmp_path / "project"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    monkeypatch.setattr(
        statusline,
        "settings_files",
        lambda p: [
            p / ".claude" / "settings.local.json",
            p / ".claude" / "settings.json",
            home / "settings.json",
        ],
    )

    def write(user: dict | None = None, local: dict | None = None) -> Path:
        for path, data in (
            (home / "settings.json", user),
            (project / ".claude" / "settings.local.json", local),
        ):
            if data is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data))
        return project

    return write


def test_the_limit_is_the_model_window_when_nothing_is_set(settings) -> None:
    project = settings(user={})
    assert statusline.compact_limit(project, "claude-opus-5-5", 1_000_000) == 1_000_000


def test_a_per_model_window_wins_over_the_top_level_one(settings) -> None:
    project = settings(
        user={
            "autoCompactWindow": 300_000,
            "modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 500_000}},
        }
    )
    # "[1m]" is how Claude Code names the long-context variant.
    assert statusline.compact_limit(project, "claude-opus-5-5[1m]", 1_000_000) == 500_000
    assert statusline.compact_limit(project, "claude-sonnet-5-5", 1_000_000) == 300_000


def test_the_project_file_wins_over_the_user_file(settings) -> None:
    """wtx writes auto_compact_window into the worktree's settings.local.json."""
    project = settings(
        user={"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 500_000}}},
        local={"autoCompactWindow": 200_000},
    )
    assert statusline.compact_limit(project, "claude-opus-5-5", 1_000_000) == 200_000


def test_the_limit_is_never_bigger_than_the_window(settings) -> None:
    project = settings(user={"autoCompactWindow": 500_000})
    assert statusline.compact_limit(project, "claude-haiku-5-5", 200_000) == 200_000


def test_auto_means_the_model_window_and_off_means_none(settings) -> None:
    project = settings(user={"autoCompactWindow": "auto"})
    assert statusline.compact_limit(project, "m", 1_000_000) == 1_000_000
    project = settings(user={"autoCompactEnabled": False, "autoCompactWindow": 300_000})
    assert statusline.compact_limit(project, "m", 1_000_000) is None


# -- the line --------------------------------------------------------------------


def _payload(cwd: Path, **over) -> dict:
    data = {
        "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
        "effort": {"level": "high"},
        "workspace": {
            "current_dir": str(cwd),
            "project_dir": str(cwd),
            "repo": {"host": "github.com", "owner": "org", "name": "app"},
        },
        "context_window": {
            "context_window_size": 1_000_000,
            "current_usage": {
                "input_tokens": 2_000,
                "cache_creation_input_tokens": 8_000,
                "cache_read_input_tokens": 90_000,
            },
        },
    }
    data.update(over)
    return data


def test_the_line_says_model_effort_and_context(settings) -> None:
    project = settings(user={"autoCompactWindow": 500_000})
    project.mkdir(parents=True, exist_ok=True)
    line = statusline.render(_payload(project))
    assert "Opus 5.5" in line and "high" in line
    assert "100k/500k" in line and "20% before compact" in line


def test_a_broken_payload_still_prints_a_line(monkeypatch, capsys) -> None:
    """The status line runs after every message. A traceback there is worse
    than a short line."""
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{not json"))
    assert statusline.main() == 0
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"model": 3}'))
    assert statusline.main() == 0
    assert "Traceback" not in capsys.readouterr().out


def test_the_branch_is_a_link_only_once_it_is_pushed(repo: Path) -> None:
    """Before the push, the link would open a 404."""
    subprocess.run(["git", "switch", "-q", "-c", "feat/link"], cwd=repo, check=True)
    line = statusline.render(_payload(repo))
    assert "feat/link" in line
    assert "github.com/org/app/tree/feat/link" not in line

    commit(repo, "x")
    subprocess.run(["git", "push", "-q", "origin", "feat/link"], cwd=repo, check=True)
    line = statusline.render(_payload(repo))
    assert "\033]8;;https://github.com/org/app/tree/feat/link\033\\feat/link" in line


def test_the_open_pr_is_a_link(repo: Path) -> None:
    pr = {"number": 29, "url": "https://github.com/org/app/pull/29"}
    line = statusline.render(_payload(repo, pr=pr))
    assert "\033]8;;https://github.com/org/app/pull/29\033\\PR #29" in line


def test_a_gitlab_branch_link_has_the_dash() -> None:
    repo = {"host": "gitlab.com", "owner": "g/sub", "name": "app"}
    assert statusline.branch_url(repo, "feat/x") == "https://gitlab.com/g/sub/app/-/tree/feat/x"


def test_tokens_are_short() -> None:
    assert statusline.tokens(84_321) == "84k"
    assert statusline.tokens(1_000_000) == "1M"
    assert statusline.tokens(1_500_000) == "1.5M"


# -- the phase -------------------------------------------------------------------


def test_a_dry_run_records_no_phase() -> None:
    from wtx import proc

    proc.set_dry_run(True)
    notify.remember_phase("app/feat-x", "plan")
    proc.set_dry_run(False)
    assert notify.phase("app/feat-x") == ""


def test_the_phase_goes_from_plan_to_build_and_teardown_forgets_it(
    wtx_repo: Path, fake_bin: Path, monkeypatch, no_fork: list
) -> None:
    from wtx import teardown

    _enable_orchestration(wtx_repo, plan_model="fable", build_model="opus")
    path = make_worktree(wtx_repo, "feat/phase")
    ctx = context.load(root=path)
    setup_mod.run_setup(ctx, start_tmux=True)

    monkeypatch.chdir(wtx_repo)
    run(["brief", "feat/phase", "--prompt", "Add a health endpoint."])
    assert notify.phase(ctx.session) == "plan"

    _accept(path, "1. write it\n\nwtx-effort: high\n")
    assert notify.phase(ctx.session) == "build"

    teardown.run_teardown(ctx)
    assert notify.phase(ctx.session) == ""


def test_a_brief_that_starts_in_auto_mode_is_a_build(
    wtx_repo: Path, fake_bin: Path, monkeypatch
) -> None:
    path = make_worktree(wtx_repo, "feat/direct")
    ctx = context.load(root=path)
    setup_mod.run_setup(ctx, start_tmux=True)
    text = (
        (wtx_repo / "wtx.toml")
        .read_text()
        .replace("[agent]", '[agent]\nbrief_permission_mode = "auto"', 1)
    )
    (wtx_repo / "wtx.toml").write_text(text)

    monkeypatch.chdir(wtx_repo)
    run(["brief", "feat/direct", "--prompt", "Fix the typo."])
    assert notify.phase(ctx.session) == "build"


def test_review_wins_while_the_notes_match_the_branch(wtx_repo: Path, fake_bin: Path) -> None:
    from wtx import review

    path = make_worktree(wtx_repo, "feat/rev")
    ctx = context.load(root=path)
    head = review.head(path)
    notify.remember_phase(ctx.session, "build")
    assert statusline.wtx_phase(ctx.session, head) == "build"

    review.save(
        ctx.session,
        {"version": 1, "summary": "s", "files": []},
        head=head,
        rng="a..b",
        agent="claude",
    )
    assert statusline.wtx_phase(ctx.session, head) == "review"
    # The agent committed something since: the notes are old, back to build.
    assert statusline.wtx_phase(ctx.session, "other") == "build"


# -- landed or not -----------------------------------------------------------------


def test_the_line_says_when_the_branch_is_landed(wtx_repo: Path, fake_bin: Path) -> None:
    """The human looks at a session and must know if its work is in the base
    yet, without running anything."""
    path = make_worktree(wtx_repo, "feat/land")
    # A fresh branch is not landed, even if the base contains its HEAD.
    assert "land" not in statusline.render(_payload(path)).split("feat/land", 1)[1]

    commit(path, "one")
    commit(path, "two")
    assert "2 to land in dev" in statusline.render(_payload(path))

    # wtx land --merge: a --no-ff merge into the local base.
    subprocess.run(["git", "switch", "-q", "dev"], cwd=wtx_repo, check=True)
    subprocess.run(
        ["git", "merge", "-q", "--no-ff", "--no-edit", "feat/land"], cwd=wtx_repo, check=True
    )
    assert "landed in dev" in statusline.render(_payload(path))

    # The agent goes on after the land: that new work is not in yet.
    commit(path, "three")
    assert "1 to land in dev" in statusline.render(_payload(path))


def test_a_branch_without_a_reflog_is_landed_by_its_merge(wtx_repo: Path, fake_bin: Path) -> None:
    path = make_worktree(wtx_repo, "feat/nolog")
    commit(path, "one")
    subprocess.run(["git", "switch", "-q", "dev"], cwd=wtx_repo, check=True)
    subprocess.run(
        ["git", "merge", "-q", "--no-ff", "--no-edit", "feat/nolog"], cwd=wtx_repo, check=True
    )
    (wtx_repo / ".git" / "logs" / "refs" / "heads" / "feat" / "nolog").unlink()
    assert "landed in dev" in statusline.render(_payload(path))

    fresh = make_worktree(wtx_repo, "feat/fresh")
    (wtx_repo / ".git" / "logs" / "refs" / "heads" / "feat" / "fresh").unlink()
    assert "landed" not in statusline.render(_payload(fresh))
