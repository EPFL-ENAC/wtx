"""End to end against a real temp git repo, with faked wt, tmux and agents."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import calls_of

from wtx import config as config_mod
from wtx import context, envfile, git, guard
from wtx import init as init_mod
from wtx import setup as setup_mod
from wtx.cli import main


def run(args: list[str]) -> int:
    return main(args)


def git_out(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    ).stdout.strip()


def make_worktree(repo: Path, branch: str) -> Path:
    """A worktree in the layout wt would make, without needing wt installed."""
    path = repo / ".claude" / "worktrees" / branch.replace("/", "-")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", branch, str(path), "origin/dev"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return path


# -- init ---------------------------------------------------------------------


def test_init_writes_a_config_that_validates(repo: Path) -> None:
    answers = init_mod.scan(repo)
    answers.pop("_notes")
    init_mod.write_all(repo, answers)
    cfg = config_mod.load(repo / "wtx.toml")
    assert config_mod.validate(cfg) == []
    assert cfg.repo.base_branch == "dev"
    assert [f.name for f in cfg.ports.families] == ["backend", "frontend"]
    assert cfg.ports.families[0].main == 8000
    assert cfg.ports.families[1].main == 5173


def test_init_finds_the_python_pin_and_the_install_steps(repo: Path) -> None:
    answers = init_mod.scan(repo)
    assert answers["deps"]["python_version_file"] == ".python-version"
    runs = [s["run"] for s in answers["deps"]["step"]]
    assert "npm ci" in runs
    assert any("uv sync" in r and "{python_version}" in r for r in runs)


def test_init_writes_a_three_line_wt_toml(wtx_repo: Path) -> None:
    body = (wtx_repo / ".wt.toml").read_text()
    for event in ("post-create", "post-checkout", "pre-remove"):
        assert f"wtx hook {event}" in body


def test_init_does_not_overwrite_without_force(wtx_repo: Path) -> None:
    with pytest.raises(FileExistsError):
        init_mod.write_all(wtx_repo, init_mod.scan(wtx_repo))


def test_edit_answers_keeps_what_the_file_chose(repo: Path) -> None:
    """A re-run must not reset what the human wrote into wtx.toml."""
    answers = init_mod.scan(repo)
    answers.pop("_notes", None)
    init_mod.write_all(repo, answers)
    text = (repo / "wtx.toml").read_text()
    protect = (
        "protected_branches = ["
        + ", ".join(f'"{b}"' for b in answers["repo"]["protected_branches"])
        + "]"
    )
    assert protect in text
    text = text.replace(protect, 'protected_branches = ["dev", "stage"]')
    assert 'plan_model = "fable"' in text
    text = text.replace('plan_model = "fable"', 'plan_model = "grid"')
    (repo / "wtx.toml").write_text(text)

    merged = init_mod.edit_answers(repo)
    assert merged["repo"]["protected_branches"] == ["dev", "stage"]
    assert merged["agent"]["orchestration"]["plan_model"] == "grid"
    # the keys the old file does not name come from the scan
    assert merged["agent"]["orchestration"]["build_model"] == "opus"
    assert merged["deps"]["python_version_file"] == ".python-version"


def test_edit_answers_reports_where_the_repo_moved_on(repo: Path) -> None:
    answers = init_mod.scan(repo)
    answers.pop("_notes", None)
    init_mod.write_all(repo, answers)

    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "deploy.yml").write_text(
        "name: deploy\non:\n  push:\n    branches:\n      - stage\njobs:\n  build:\n"
    )
    (repo / "backend" / "Makefile").write_text("BACKEND_PORT ?= 8010\ndev:\n\trun:\n")

    merged = init_mod.edit_answers(repo)
    marked = {c["what"]: c for c in merged["_changes"]}
    assert marked["repo.protected_branches"]["scan"] == ["dev", "stage"]
    assert marked["ports.backend.main"]["file"] == 8000
    assert marked["ports.backend.main"]["scan"] == 8010
    assert marked["panes.backend.cmd"]["file"] == "make run"
    assert marked["panes.backend.cmd"]["scan"] == "make dev"
    # the file's own values are what comes out, the skill asks before moving
    assert merged["ports"]["family"][0]["main"] == 8000


def test_edit_answers_reports_a_service_the_repo_grew(repo: Path) -> None:
    """The file was written before the frontend existed. Keeping the file's
    lists is right, but staying quiet about it means nobody ever adds the
    port and the pane."""
    answers = init_mod.scan(repo)
    answers.pop("_notes", None)
    answers["ports"]["family"] = [f for f in answers["ports"]["family"] if f["name"] != "frontend"]
    answers["panes"]["pane"] = [p for p in answers["panes"]["pane"] if p["name"] != "frontend"]
    init_mod.write_all(repo, answers)

    merged = init_mod.edit_answers(repo)
    marked = {c["what"]: c for c in merged["_changes"]}
    assert marked["ports.frontend"]["file"] is None
    assert marked["ports.frontend"]["scan"]["name"] == "frontend"
    assert marked["panes.frontend"]["file"] is None
    # the file's own lists still win, the skill asks before adding
    assert [f["name"] for f in merged["ports"]["family"]] == ["backend"]


def test_edit_answers_stays_quiet_about_what_the_file_dropped(repo: Path) -> None:
    """A pane the human deleted is a choice, not drift."""
    answers = init_mod.scan(repo)
    answers.pop("_notes", None)
    init_mod.write_all(repo, answers)
    text = (repo / "wtx.toml").read_text()
    (repo / "wtx.toml").write_text(text)

    merged = init_mod.edit_answers(repo)
    assert merged["_changes"] == []


def test_edit_answers_without_wtx_toml_is_a_plain_scan(repo: Path) -> None:
    merged = init_mod.edit_answers(repo)
    assert merged["_changes"] == []
    assert merged["repo"]["name"] == "remote"
    assert merged["agent"]["tool"] == "claude"


def test_edit_answers_on_a_broken_toml_says_what(repo: Path) -> None:
    (repo / "wtx.toml").write_text("not = [valid\n")
    with pytest.raises(ValueError):
        init_mod.edit_answers(repo)


def test_edit_answers_write_back_through_force(repo: Path) -> None:
    """The re-run flow: merge, ask, write with --force, still valid."""
    answers = init_mod.scan(repo)
    answers.pop("_notes", None)
    init_mod.write_all(repo, answers)
    text = (
        (repo / "wtx.toml")
        .read_text()
        .replace('base_branch = "dev"', 'base_branch = "main"')
        .replace('protected_branches = ["dev"]', 'protected_branches = ["main", "dev"]')
    )
    (repo / "wtx.toml").write_text(text)

    merged = init_mod.edit_answers(repo)
    merged.pop("_notes", None)
    merged.pop("_changes", None)
    init_mod.write_all(repo, merged, force=True)

    cfg = config_mod.load(repo / "wtx.toml")
    assert config_mod.validate(cfg) == []
    assert cfg.repo.base_branch == "main"


def test_init_edit_prints_a_plan_and_writes_nothing(
    wtx_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(wtx_repo)
    before = (wtx_repo / "wtx.toml").read_text()
    assert main(["init", "--edit"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["_changes"] == []
    assert out["repo"]["name"] == "remote"
    assert (wtx_repo / "wtx.toml").read_text() == before


@pytest.mark.parametrize(
    "flags",
    [
        ["--edit", "--scan"],
        ["--edit", "--from-json", "x.json"],
        ["--scan", "--from-json", "x.json"],
    ],
)
def test_init_takes_one_source_at_a_time(
    wtx_repo: Path, monkeypatch: pytest.MonkeyPatch, flags: list
) -> None:
    """Silently ignoring one of them would write from answers nobody saw."""
    monkeypatch.chdir(wtx_repo)
    with pytest.raises(SystemExit):
        main(["init", *flags])


# -- setup --------------------------------------------------------------------


def test_setup_writes_ports_settings_and_the_guard(wtx_repo: Path, fake_bin: Path) -> None:
    path = make_worktree(wtx_repo, "feat/one")
    ctx = context.load(root=path)
    setup_mod.run_setup(ctx, start_tmux=False)

    values = envfile.read_worktree(path)
    assert values["WT_BRANCH"] == "feat/one"
    assert values["WT_SLUG"] == "feat-one"
    assert 18000 <= int(values["BACKEND_PORT"]) < 18500
    assert 19000 <= int(values["FRONTEND_PORT"]) < 19500

    settings = json.loads((path / ".claude/settings.local.json").read_text())
    assert settings["autoCompactWindow"] == 200000
    assert settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "sonnet"
    assert "Bash(git push * dev)" in settings["permissions"]["deny"]
    assert guard.is_installed(wtx_repo)


def test_setup_is_idempotent(wtx_repo: Path, fake_bin: Path) -> None:
    """wt fires post_create and post_checkout, and some builds fire both."""
    path = make_worktree(wtx_repo, "feat/twice")
    ctx = context.load(root=path)
    setup_mod.run_setup(ctx, start_tmux=False)
    first = (path / ".env.worktree").read_text()
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    assert (path / ".env.worktree").read_text() == first


def test_two_worktrees_never_share_a_port(wtx_repo: Path, fake_bin: Path) -> None:
    ports: list[set[str]] = []
    for branch in ("feat/a", "feat/b", "feat/c"):
        path = make_worktree(wtx_repo, branch)
        setup_mod.run_setup(context.load(root=path), start_tmux=False)
        values = envfile.read_worktree(path)
        ports.append({values["BACKEND_PORT"], values["FRONTEND_PORT"]})
    assert ports[0] & ports[1] == set()
    assert ports[0] & ports[2] == set()
    assert ports[1] & ports[2] == set()


def test_setup_repairs_a_branch_that_tracks_its_base(wtx_repo: Path, fake_bin: Path) -> None:
    """wt create leaves the new branch tracking origin/dev. Then git pull
    rebases the work onto dev and the next push is refused."""
    path = make_worktree(wtx_repo, "feat/tracked")
    subprocess.run(
        ["git", "branch", "--set-upstream-to", "origin/dev"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    upstream = git_out(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], path)
    assert upstream != "origin/dev"


def test_setup_keeps_a_key_a_hook_wrote(wtx_repo: Path, fake_bin: Path) -> None:
    path = make_worktree(wtx_repo, "feat/keep")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    f = path / ".env.worktree"
    f.write_text(f.read_text() + "\n# set by hand\nMODEL_PROFILE=tcaf\n")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    assert envfile.read(f)["MODEL_PROFILE"] == "tcaf"
    assert "# set by hand" in f.read_text()


def test_seeded_file_is_not_overwritten(wtx_repo: Path, fake_bin: Path) -> None:
    (wtx_repo / "CLAUDE.md").write_text("from main\n")
    path = make_worktree(wtx_repo, "feat/seed")
    ctx = context.load(root=path)
    ctx.cfg = config_mod.parse(
        {**{"seed": {"copy": ["CLAUDE.md"]}}, "repo": {"base_branch": "dev"}}
    )
    setup_mod.run_setup(ctx, start_tmux=False)
    assert (path / "CLAUDE.md").read_text() == "from main\n"
    (path / "CLAUDE.md").write_text("changed here\n")
    setup_mod.run_setup(ctx, start_tmux=False)
    assert (path / "CLAUDE.md").read_text() == "changed here\n"


# -- the guard ----------------------------------------------------------------


@pytest.fixture
def guarded(wtx_repo: Path, fake_bin: Path) -> Path:
    path = make_worktree(wtx_repo, "feat/guard")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "work"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path


def push(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "push", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def test_worktree_may_push_its_own_branch(guarded: Path) -> None:
    assert push(guarded, "origin", "HEAD:refs/heads/feat/guard").returncode == 0


@pytest.mark.parametrize(
    "args",
    [
        ("origin", "HEAD:dev"),
        ("origin", "HEAD:main"),
        ("--dry-run", "origin", "HEAD:dev"),
        ("--force", "origin", "HEAD:dev"),
        ("origin", "HEAD:refs/heads/other"),
    ],
)
def test_worktree_may_not_push_anything_else(guarded: Path, args) -> None:
    """The --dry-run case is the one the old lefthook guard let through."""
    result = push(guarded, *args)
    assert result.returncode != 0
    assert "push-guard" in result.stderr


def test_main_checkout_is_unrestricted(guarded: Path, wtx_repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "on dev"],
        cwd=wtx_repo,
        check=True,
        capture_output=True,
    )
    assert push(wtx_repo, "origin", "dev").returncode == 0


def test_an_existing_pre_push_hook_still_runs(wtx_repo: Path, fake_bin: Path) -> None:
    hooks = guard.hooks_dir(wtx_repo)
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-push").write_text("#!/bin/sh\necho CHAINED >&2\nexit 0\n")
    (hooks / "pre-push").chmod(0o755)
    cfg = config_mod.load(wtx_repo / "wtx.toml")
    guard.install(cfg, wtx_repo)
    assert (hooks / "pre-push.before-wt").is_file()

    path = make_worktree(wtx_repo, "feat/chain")
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "w"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    result = push(path, "origin", "HEAD:refs/heads/feat/chain")
    assert result.returncode == 0
    assert "CHAINED" in result.stderr


def test_installing_twice_does_not_chain_the_guard_to_itself(
    wtx_repo: Path, fake_bin: Path
) -> None:
    cfg = config_mod.load(wtx_repo / "wtx.toml")
    guard.install(cfg, wtx_repo)
    guard.install(cfg, wtx_repo)
    hooks = guard.hooks_dir(wtx_repo)
    assert not (hooks / "pre-push.before-wt").is_file()


# -- external repos -----------------------------------------------------------


def _add_repos_block(root: Path, sibling: Path) -> None:
    (root / "wtx.toml").write_text(
        (root / "wtx.toml").read_text()
        + f'''
[[repos]]
name = "model"
path = "{sibling}"
access = "pair"
base_branch = "main"
env_prefix = "TCM"
editable_install = {{ cwd = "backend", run = "uv pip install -e {{path}}" }}

[[repos]]
name = "conf"
path = "{sibling.parent / "conf"}"
access = "read"
'''
    )
    (sibling.parent / "conf").mkdir(exist_ok=True)
    (sibling.parent / "conf" / "app.yaml").write_text("kind: Deployment\n")


def test_a_read_repo_is_readable_and_never_writable(
    wtx_repo: Path, sibling: Path, fake_bin: Path
) -> None:
    _add_repos_block(wtx_repo, sibling)
    path = make_worktree(wtx_repo, "feat/read")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    settings = json.loads((path / ".claude/settings.local.json").read_text())
    conf = str(sibling.parent / "conf")
    assert conf in settings["permissions"]["additionalDirectories"]
    assert f"Edit(//{conf.lstrip('/')}/**)" in settings["permissions"]["deny"]
    assert f"{conf}/**" in settings["sandbox"]["filesystem"]["denyWrite"]


def test_pairing_makes_a_worktree_in_the_other_repo(
    wtx_repo: Path, sibling: Path, fake_bin: Path
) -> None:
    _add_repos_block(wtx_repo, sibling)
    path = make_worktree(wtx_repo, "feat/pair")
    ctx = context.load(root=path)
    setup_mod.run_setup(ctx, with_repos={"model": "feat/pair-model"}, start_tmux=False)

    values = envfile.read_worktree(path)
    assert values["TCM_BRANCH"] == "feat/pair-model"
    assert Path(values["TCM_PATH"]).is_dir()
    assert values["UV_NO_SYNC"] == "1"
    branches = [w.branch for w in git.list_worktrees(sibling)]
    assert "feat/pair-model" in branches

    settings = json.loads((path / ".claude/settings.local.json").read_text())
    paired = values["TCM_PATH"]
    assert paired in settings["permissions"]["additionalDirectories"]
    assert f"Edit(//{paired.lstrip('/')}/**)" in settings["permissions"]["allow"]
    assert paired in settings["sandbox"]["filesystem"]["allowWrite"]
    assert str(path) in settings["sandbox"]["filesystem"]["allowWrite"]


def test_a_pairing_is_remembered_on_the_next_setup(
    wtx_repo: Path, sibling: Path, fake_bin: Path
) -> None:
    """A plain `wtx go` must never quietly un-pair a worktree."""
    _add_repos_block(wtx_repo, sibling)
    path = make_worktree(wtx_repo, "feat/sticky")
    setup_mod.run_setup(
        context.load(root=path), with_repos={"model": "feat/sticky-model"}, start_tmux=False
    )
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    assert envfile.read_worktree(path)["TCM_BRANCH"] == "feat/sticky-model"


def test_an_unpaired_pair_repo_is_read_only(wtx_repo: Path, sibling: Path, fake_bin: Path) -> None:
    _add_repos_block(wtx_repo, sibling)
    path = make_worktree(wtx_repo, "feat/unpaired")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    settings = json.loads((path / ".claude/settings.local.json").read_text())
    main_path = str(sibling)
    assert main_path in settings["permissions"]["additionalDirectories"]
    assert f"Edit(//{main_path.lstrip('/')}/**)" in settings["permissions"]["deny"]


def test_teardown_keeps_the_paired_worktree(wtx_repo: Path, sibling: Path, fake_bin: Path) -> None:
    from wtx.teardown import run_teardown

    _add_repos_block(wtx_repo, sibling)
    path = make_worktree(wtx_repo, "feat/keeppair")
    setup_mod.run_setup(
        context.load(root=path), with_repos={"model": "feat/keep-model"}, start_tmux=False
    )
    run_teardown(context.load(root=path))
    assert "feat/keep-model" in [w.branch for w in git.list_worktrees(sibling)]


# -- opencode -----------------------------------------------------------------


def test_opencode_backend_writes_its_own_config(wtx_repo: Path, fake_bin: Path) -> None:
    path = make_worktree(wtx_repo, "feat/oc")
    setup_mod.run_setup(
        context.load(root=path), agent_tool="opencode", llm="qwen", start_tmux=False
    )
    data = json.loads((path / "opencode.json").read_text())
    assert data["model"] == "qwen"
    assert data["permission"]["bash"]["git push * dev"] == "deny"
    assert data["permission"]["*"] == "ask"
    assert envfile.read_worktree(path)["WTX_AGENT"] == "opencode"


def test_opencode_orchestration_writes_a_model_per_agent(wtx_repo: Path, fake_bin: Path) -> None:
    """The TUI switches agents itself when the plan is accepted, and the model
    follows the agent through `mode`: the top level model stays the
    worktree's own one."""
    _enable_orchestration(wtx_repo, plan_model='"planner"', build_model='"worker"')
    path = make_worktree(wtx_repo, "feat/oc-plan")
    setup_mod.run_setup(
        context.load(root=path), agent_tool="opencode", llm="qwen", start_tmux=False
    )
    data = json.loads((path / "opencode.json").read_text())
    assert data["model"] == "qwen"
    assert data["agent"] == {"plan": {"model": "planner"}, "build": {"model": "worker"}}


# -- tmux ---------------------------------------------------------------------


def test_a_session_is_built_with_one_pane_per_config_entry(wtx_repo: Path, fake_bin: Path) -> None:
    path = make_worktree(wtx_repo, "feat/tmux")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)
    roles = [c for c in calls_of(fake_bin, "tmux") if "@wt_role" in c]
    assert len(roles) == 4
    assert any(c.endswith("@wt_role agent") for c in roles)
    scrub = [c for c in calls_of(fake_bin, "tmux") if "set-environment -gu" in c]
    assert any("ROOT" in c for c in scrub)
    assert any("BACKEND_PORT" in c for c in scrub)


def test_the_session_name_has_no_dots(wtx_repo: Path, fake_bin: Path) -> None:
    path = make_worktree(wtx_repo, "release/1.2")
    ctx = context.load(root=path)
    assert ctx.session.endswith("release/1-2")


# -- cli ----------------------------------------------------------------------


def test_status_reports_ports_and_pairings(
    wtx_repo: Path, sibling: Path, fake_bin: Path, capsys, monkeypatch
) -> None:
    _add_repos_block(wtx_repo, sibling)
    path = make_worktree(wtx_repo, "feat/status")
    setup_mod.run_setup(
        context.load(root=path), with_repos={"model": "feat/status-model"}, start_tmux=False
    )
    monkeypatch.chdir(wtx_repo)
    assert run(["status", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    row = next(r for r in rows if r["branch"] == "feat/status")
    assert row["ports"]["backend"].isdigit()
    assert row["repos"]["model"]["branch"] == "feat/status-model"


def test_go_refuses_a_protected_branch(wtx_repo: Path, fake_bin: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(wtx_repo)
    assert run(["go", "dev"]) == 1
    assert "protected" in capsys.readouterr().err


def test_land_refuses_from_a_worktree(wtx_repo: Path, fake_bin: Path, monkeypatch, capsys) -> None:
    path = make_worktree(wtx_repo, "feat/land")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    monkeypatch.chdir(path)
    assert run(["land", "feat/land"]) == 1
    assert "main checkout" in capsys.readouterr().err


def test_dry_run_changes_nothing(wtx_repo: Path, fake_bin: Path, monkeypatch, capsys) -> None:
    path = make_worktree(wtx_repo, "feat/dry")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "w"], cwd=path, check=True)
    before = git_out(["rev-parse", "dev"], wtx_repo)
    monkeypatch.chdir(wtx_repo)
    run(["--dry-run", "land", "feat/dry", "--local"])
    out = capsys.readouterr().out
    assert "would run" in out
    assert git_out(["rev-parse", "dev"], wtx_repo) == before
    assert git.worktree_path_for(wtx_repo, "feat/dry") is not None


def test_config_validate_reports_a_problem(wtx_repo: Path, monkeypatch, capsys) -> None:
    (wtx_repo / "wtx.toml").write_text(
        '[repo]\nname = "x"\nbase_branch = "dev"\nprotected_branches = ["main"]\n'
    )
    monkeypatch.chdir(wtx_repo)
    assert run(["config", "validate", "--json"]) == 1
    assert "base branch" in capsys.readouterr().out


def test_doctor_runs_and_reports(wtx_repo: Path, fake_bin: Path, monkeypatch, capsys) -> None:
    from wtx import doctor

    monkeypatch.chdir(wtx_repo)
    report = doctor.run(wtx_repo)
    names = {c.name for c in report.checks}
    assert "push guard installed" in names
    assert "config is valid" in names


def test_go_refuses_an_invalid_config(wtx_repo: Path, fake_bin: Path, monkeypatch, capsys) -> None:
    """Setting a worktree up wrong is worse than not setting it up: it writes
    permissions, installs a guard and starts servers from unchecked values."""
    (wtx_repo / "wtx.toml").write_text(
        (wtx_repo / "wtx.toml").read_text()
        + '\n[[repos]]\nname = "a"\npath = "../x"\n\n[[repos]]\nname = "a"\npath = "../y"\n'
    )
    monkeypatch.chdir(wtx_repo)
    assert run(["go", "feat/bad"]) == 1
    err = capsys.readouterr().err
    assert "has problems" in err
    assert git.worktree_path_for(wtx_repo, "feat/bad") is None


def test_notify_writes_a_state_and_the_status_line(
    wtx_repo: Path, fake_bin: Path, monkeypatch
) -> None:
    from wtx import notify

    path = make_worktree(wtx_repo, "feat/notify")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)
    ctx = context.load(root=path)
    monkeypatch.setattr(notify, "session_for", lambda cwd: ctx.session)
    monkeypatch.setattr(notify, "_spawn_desktop", lambda *a, **k: None)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda s: True})())
    notify.handle("permission", cwd=path)

    assert notify.read_state(ctx.session)["state"] == "permission"
    assert ctx.session in notify.status_line()
    notify.handle("running", cwd=path)
    assert notify.read_state(ctx.session) == {}
    assert notify.status_line() == ""


def test_a_settled_session_drops_its_notification(
    wtx_repo: Path, fake_bin: Path, monkeypatch
) -> None:
    """Every permission prompt used to leave a banner in the GNOME list. A day
    of agents filled it, and a full list makes the shell crawl."""
    from wtx import notify

    path = make_worktree(wtx_repo, "feat/close")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)
    ctx = context.load(root=path)
    monkeypatch.setattr(notify, "session_for", lambda cwd: ctx.session)
    monkeypatch.setattr(notify, "_spawn_desktop", lambda *a, **k: None)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda s: True})())

    # pid 0 is nobody, so nothing is killed. The id is what matters here.
    notify.id_file(ctx.session).write_text("7 0")
    notify.handle("permission", cwd=path)
    notify.handle("running", cwd=path)

    closed = [c for c in calls_of(fake_bin, "gdbus") if "CloseNotification" in c]
    assert len(closed) == 1
    assert closed[0].endswith(" 7")
    assert not notify.id_file(ctx.session).exists()


def test_a_dead_session_drops_its_notification(wtx_repo: Path, fake_bin: Path, monkeypatch) -> None:
    """A banner for a worktree that no longer exists sits in the list for ever,
    and clicking it opens nothing."""
    from wtx import notify, teardown

    path = make_worktree(wtx_repo, "feat/gone")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)
    ctx = context.load(root=path)
    notify.write_state(ctx.session, "permission")
    notify.id_file(ctx.session).write_text("9 0")

    # What the pre_remove hook runs, before the worktree goes.
    teardown.run_teardown(ctx)

    closed = [c for c in calls_of(fake_bin, "gdbus") if "CloseNotification" in c]
    assert closed and closed[-1].endswith(" 9")
    assert notify.read_state(ctx.session) == {}
    assert not notify.id_file(ctx.session).exists()


def test_the_grid_shows_the_waiting_sessions_first(
    wtx_repo: Path, fake_bin: Path, monkeypatch
) -> None:
    """One tile per session, and the one asking for something is tile one."""
    import os

    from wtx import monitor, notify

    Path(os.environ["WTX_TEST_SESSIONS"]).write_text("app/calm\napp/asking\n")
    notify.write_state("app/asking", "permission")

    monitor._build_grid()
    tmux_calls = calls_of(fake_bin, "tmux")
    windows = [c for c in tmux_calls if c.startswith("new-window")]
    splits = [c for c in tmux_calls if c.startswith("split-window")]
    assert len(windows) == 1 and "app/asking" in windows[0]
    assert len(splits) == 1 and "app/calm" in splits[0]
    assert any(c.startswith("select-layout") and c.endswith("tiled") for c in tmux_calls)
    assert any("pane-border-status top" in c for c in tmux_calls)


def test_the_grid_never_tiles_the_monitor_itself(
    wtx_repo: Path, fake_bin: Path, monkeypatch
) -> None:
    """A tile peeking at the monitor session draws the grid inside the grid."""
    import os

    from wtx import monitor

    Path(os.environ["WTX_TEST_SESSIONS"]).write_text(f"{monitor.SESSION}\napp/one\n")
    monitor._build_grid()
    peeked = [c.split("--peek ", 1)[1] for c in calls_of(fake_bin, "tmux") if "--peek " in c]
    assert peeked == ["app/one"]


def test_wt_toml_hooks_are_lists(wtx_repo: Path) -> None:
    """wt runs a hook given as a list. A plain string is accepted by the TOML
    parser and then silently never runs, so a whole repo looks set up and is
    not."""
    import tomllib

    data = tomllib.loads((wtx_repo / ".wt.toml").read_text())
    for event in ("post_create", "post_checkout", "pre_remove"):
        value = data["hooks"][event]
        assert isinstance(value, list), f"{event} must be a list, got {type(value)}"
        assert value[0].startswith("wtx hook ")


def test_with_alone_pairs_on_the_app_branch(wtx_repo: Path, sibling: Path, fake_bin: Path) -> None:
    _add_repos_block(wtx_repo, sibling)
    path = make_worktree(wtx_repo, "feat/same")
    setup_mod.run_setup(context.load(root=path), with_repos={"model": ""}, start_tmux=False)
    assert envfile.read_worktree(path)["TCM_BRANCH"] == "feat/same"
    assert "feat/same" in [w.branch for w in git.list_worktrees(sibling)]


def test_hook_ignores_another_worktrees_exported_values(
    wtx_repo: Path, fake_bin: Path, monkeypatch
) -> None:
    """Run from a pane where another worktree's .env.worktree is exported,
    the hook must not hand that worktree's model to the new one."""
    from wtx.hooks import GO_LLM_ENV, run_hook

    path = make_worktree(wtx_repo, "feat/leak")
    monkeypatch.setenv("WT_PATH", str(path))
    monkeypatch.setenv("WT_BRANCH", "feat/leak")
    monkeypatch.setenv("WTX_LLM", "sonnet-from-elsewhere")
    monkeypatch.setenv("WTX_AGENT", "opencode")
    assert run_hook("post-checkout") == 0
    values = envfile.read_worktree(path)
    assert values["WTX_LLM"] == "opus"  # the config's own value, not the leaked one
    assert values["WTX_AGENT"] == "claude"

    monkeypatch.setenv(GO_LLM_ENV, "haiku")
    assert run_hook("post-checkout") == 0
    assert envfile.read_worktree(path)["WTX_LLM"] == "haiku"


def test_land_rebases_when_the_base_moved_on(
    wtx_repo: Path, fake_bin: Path, monkeypatch, capsys
) -> None:
    """The ordinary case: dev got a commit after the branch was cut."""
    path = make_worktree(wtx_repo, "feat/behind")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    (path / "feature.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "feat"], cwd=path, check=True)
    (wtx_repo / "other.txt").write_text("y\n")
    subprocess.run(["git", "add", "-A"], cwd=wtx_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "dev moved"], cwd=wtx_repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "dev"], cwd=wtx_repo, check=True)

    monkeypatch.chdir(wtx_repo)
    assert run(["land", "feat/behind", "--local", "--skip-checks"]) == 0
    assert (wtx_repo / "feature.txt").is_file()
    assert (wtx_repo / "other.txt").is_file()
    assert git.worktree_path_for(wtx_repo, "feat/behind") is None


def test_land_refuses_a_branch_cut_from_a_newer_protected_branch(
    wtx_repo: Path, fake_bin: Path, monkeypatch, capsys
) -> None:
    """stage is ahead of dev and the branch contains all of stage."""
    cfg_path = wtx_repo / "wtx.toml"
    cfg_path.write_text(
        cfg_path.read_text().replace(
            'protected_branches = ["dev"]', 'protected_branches = ["dev", "stage"]'
        )
    )
    subprocess.run(["git", "commit", "-qam", "stage protected"], cwd=wtx_repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "dev"], cwd=wtx_repo, check=True)
    subprocess.run(["git", "branch", "stage"], cwd=wtx_repo, check=True)
    (wtx_repo / "stage.txt").write_text("s\n")
    subprocess.run(["git", "checkout", "-q", "stage"], cwd=wtx_repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=wtx_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "on stage"], cwd=wtx_repo, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "stage"], cwd=wtx_repo, check=True)
    subprocess.run(["git", "checkout", "-q", "dev"], cwd=wtx_repo, check=True)

    path = wtx_repo / ".claude" / "worktrees" / "feat-drag"
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feat/drag", str(path), "origin/stage"],
        cwd=wtx_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "w"], cwd=path, check=True)

    monkeypatch.chdir(wtx_repo)
    assert run(["land", "feat/drag", "--local", "--skip-checks"]) == 1
    assert "drag" in capsys.readouterr().err
    assert git.worktree_path_for(wtx_repo, "feat/drag") is not None


def test_a_setup_error_is_a_message_not_a_traceback(
    wtx_repo: Path, fake_bin: Path, monkeypatch, capsys
) -> None:
    cfg_path = wtx_repo / "wtx.toml"
    cfg_path.write_text(cfg_path.read_text() + '\n[seed]\nrequired = ["backend/.env"]\n')
    path = make_worktree(wtx_repo, "feat/seedless")
    monkeypatch.chdir(path)
    assert run(["setup", "--no-tmux"]) == 1
    err = capsys.readouterr().err
    assert "backend/.env" in err
    assert "Traceback" not in err


def test_help_is_a_word_too(capsys) -> None:
    assert run([]) == 0
    assert "go" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        run(["help", "go"])
    assert exc.value.code == 0
    assert "--with" in capsys.readouterr().out


# -- orchestration ------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_fork(monkeypatch):
    """capture() fires the handoff itself. A test must never fork the real
    process that respawns a pane, so keep the records it would have run."""
    from wtx import orchestrate

    fired: list[Path] = []
    monkeypatch.setattr(orchestrate, "_spawn", lambda record, **kw: fired.append(record) or True)
    return fired


def _enable_orchestration(root: Path, **over) -> None:
    """Replace the block `wtx init` writes, which is there but switched off."""
    keys = {"enabled": "true", **over}
    body = "\n".join(f"{k} = {v}" for k, v in keys.items())
    kept, skipping = [], False
    for line in (root / "wtx.toml").read_text().splitlines():
        if line.startswith("["):
            skipping = line.strip() == "[agent.orchestration]"
        if not skipping:
            kept.append(line)
    (root / "wtx.toml").write_text("\n".join(kept) + f"\n\n[agent.orchestration]\n{body}\n")


def _accept(path: Path, plan: str, *, conversation: str = "conv-1") -> str:
    from wtx import orchestrate

    return orchestrate.capture(
        {
            "cwd": str(path),
            "session_id": conversation,
            "tool_name": "ExitPlanMode",
            "tool_input": {"plan": plan},
        }
    )


def test_an_accepted_plan_continues_on_the_implementation_model(
    wtx_repo: Path, fake_bin: Path, no_fork: list
) -> None:
    """The whole point: plan on fable, accept, implement on opus, no typing.

    And in the same conversation, so the implementation still has everything
    the planner read.
    """
    from wtx import orchestrate

    _enable_orchestration(wtx_repo, plan_model='"fable"', build_model='"opus"')
    path = make_worktree(wtx_repo, "feat/plan")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)

    answer = json.loads(_accept(path, "1. write it\n\nwtx-size: large\n", conversation="conv-abc"))
    assert answer["continue"] is False
    assert "opus" in answer["stopReason"]

    assert len(no_fork) == 1
    orchestrate.run(no_fork[0])

    sent = [c for c in calls_of(fake_bin, "tmux") if "send-keys" in c]
    assert any(
        "claude -r conv-abc --model opus --permission-mode auto --effort xhigh" in c for c in sent
    )


def test_a_small_plan_lowers_the_effort_not_the_model(
    wtx_repo: Path, fake_bin: Path, no_fork: list
) -> None:
    from wtx import orchestrate

    _enable_orchestration(wtx_repo, build_model='"opus"', small_effort='"medium"')
    path = make_worktree(wtx_repo, "feat/small")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)

    _accept(path, "1. rename it\n\nwtx-size: small\n")
    record = json.loads(no_fork[0].read_text())
    assert record["model"] == "opus"

    orchestrate.run(no_fork[0])
    sent = [c for c in calls_of(fake_bin, "tmux") if "send-keys" in c]
    assert any("--model opus" in c and "--effort medium" in c for c in sent)


def test_a_plan_with_no_conversation_to_resume_is_not_stopped(
    wtx_repo: Path, fake_bin: Path
) -> None:
    """Stopping the agent with nothing to resume would take the plan away."""
    _enable_orchestration(wtx_repo)
    path = make_worktree(wtx_repo, "feat/no-conv")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)

    assert _accept(path, "1. do it\n\nwtx-size: large\n", conversation="") == ""


def test_a_repo_without_orchestration_is_left_alone(wtx_repo: Path, fake_bin: Path) -> None:
    """Every repo that has not opted in must see no change at all."""
    from wtx import orchestrate

    path = make_worktree(wtx_repo, "feat/plain")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)
    ctx = context.load(root=path)

    assert _accept(path, "1. do it\n\nwtx-size: large\n") == ""
    assert not orchestrate.record_file(ctx.session).exists()


def test_a_plan_the_planner_can_implement_itself_is_not_handed_over(
    wtx_repo: Path, fake_bin: Path
) -> None:
    """Same model, same effort: respawning the pane would only cost a restart."""
    _enable_orchestration(
        wtx_repo,
        plan_model='"opus"',
        plan_effort='""',
        build_model='"opus"',
        large_effort='""',
    )
    path = make_worktree(wtx_repo, "feat/same-model")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)

    assert _accept(path, "1. do it\n\nwtx-size: large\n") == ""


def test_a_handoff_fires_once_however_often_the_agent_stops(
    wtx_repo: Path, fake_bin: Path, no_fork: list
) -> None:
    """The hook fires it, then the Stop hook fires on every turn. Briefing an
    agent twice throws away the first run."""
    from wtx import orchestrate

    _enable_orchestration(wtx_repo)
    path = make_worktree(wtx_repo, "feat/once")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)
    ctx = context.load(root=path)

    _accept(path, "1. do it\n\nwtx-size: large\n")
    assert len(no_fork) == 1
    assert orchestrate.drain(ctx.session) is False
    assert orchestrate.drain(ctx.session) is False
    assert len(no_fork) == 1


def test_a_brief_asks_the_planner_for_an_effort(
    wtx_repo: Path, fake_bin: Path, monkeypatch
) -> None:
    from wtx import orchestrate

    _enable_orchestration(wtx_repo)
    path = make_worktree(wtx_repo, "feat/brief")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)

    monkeypatch.chdir(wtx_repo)
    run(["brief", "feat/brief", "--prompt", "Add a health endpoint."])
    text = (path / "PROMPT.md").read_text()
    assert "Add a health endpoint." in text
    assert orchestrate.EFFORT_MARKER in text


def test_a_briefed_session_starts_on_the_planning_model(
    wtx_repo: Path, fake_bin: Path, monkeypatch
) -> None:
    _enable_orchestration(wtx_repo, plan_model='"fable"')
    path = make_worktree(wtx_repo, "feat/planner")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)

    monkeypatch.chdir(wtx_repo)
    run(["brief", "feat/planner", "--prompt", "Add a health endpoint."])
    sent = [c for c in calls_of(fake_bin, "tmux") if "send-keys" in c]
    assert any("--model fable --permission-mode plan" in c for c in sent)


def test_the_hook_fires_the_handoff_itself(wtx_repo: Path, fake_bin: Path, no_fork: list) -> None:
    """Stopping the turn from a hook means Claude Code never runs the Stop
    hook, so a handoff left for it sits there forever. Checked against 2.1.267,
    where the record was written and nothing ever claimed it."""
    _enable_orchestration(wtx_repo)
    path = make_worktree(wtx_repo, "feat/stop")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)

    _accept(path, "1. do it\n\nwtx-size: large\n")

    assert len(no_fork) == 1


def test_the_stop_hook_still_fires_a_handoff_left_behind(
    wtx_repo: Path, fake_bin: Path, monkeypatch, no_fork: list
) -> None:
    """The backup path: a record written by an older wtx, or one whose fork
    failed. The session must not be reported as waiting on a human either."""
    from wtx import notify, orchestrate

    _enable_orchestration(wtx_repo)
    path = make_worktree(wtx_repo, "feat/left")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)
    ctx = context.load(root=path)
    monkeypatch.setattr(notify, "session_for", lambda cwd: ctx.session)
    monkeypatch.setattr(notify, "_spawn_desktop", lambda *a, **k: None)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda s: True})())

    orchestrate.write_record(
        ctx.session,
        {
            "session": ctx.session,
            "root": str(path),
            "conversation": "conv-left",
            "model": "opus",
            "size": "large",
        },
    )
    notify.handle("stop", cwd=path)

    assert len(no_fork) == 1
    assert notify.read_state(ctx.session) == {}


def test_the_handoff_command_answers_the_hook(
    wtx_repo: Path, fake_bin: Path, monkeypatch, capsys
) -> None:
    from wtx import notify

    _enable_orchestration(wtx_repo)
    path = make_worktree(wtx_repo, "feat/cmd")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)
    monkeypatch.setattr(
        notify,
        "payload_from_stdin",
        lambda: {
            "cwd": str(path),
            "session_id": "conv-1",
            "tool_input": {"plan": "1. do it\n\nwtx-size: large"},
        },
    )

    capsys.readouterr()
    assert run(["handoff"]) == 0
    assert json.loads(capsys.readouterr().out)["continue"] is False


def test_the_plan_is_read_from_the_file_when_the_tool_does_not_carry_it(
    wtx_repo: Path, fake_bin: Path, tmp_path: Path, no_fork: list
) -> None:
    """Claude Code 2.1.267 dropped `plan` from the ExitPlanMode schema: the
    plan goes to a file and the tool only says it is ready. Reading the input
    key alone makes every handoff a silent no-op."""
    from wtx import orchestrate

    _enable_orchestration(wtx_repo, build_model='"opus"')
    path = make_worktree(wtx_repo, "feat/planfile")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)
    plan_file = tmp_path / "the-plan.md"
    plan_file.write_text("1. write it\n\nwtx-size: large\n")

    answer = orchestrate.capture(
        {
            "cwd": str(path),
            "session_id": "conv-file",
            "tool_name": "ExitPlanMode",
            "tool_input": {"planFilePath": str(plan_file)},
        }
    )

    assert json.loads(answer)["continue"] is False
    record = json.loads(no_fork[0].read_text())
    assert record["size"] == "large"
    assert record["conversation"] == "conv-file"


def test_the_plan_file_can_come_from_the_tool_response(
    wtx_repo: Path, fake_bin: Path, tmp_path: Path
) -> None:
    """The accepted-plan result names it `filePath`, the call names it
    `planFilePath`. Both are in the same transcript, so read both."""
    from wtx import orchestrate

    _enable_orchestration(wtx_repo)
    path = make_worktree(wtx_repo, "feat/response")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)
    plan_file = tmp_path / "from-response.md"
    plan_file.write_text("1. do it\n\nwtx-size: small\n")

    answer = orchestrate.capture(
        {
            "cwd": str(path),
            "session_id": "conv-resp",
            "tool_name": "ExitPlanMode",
            "tool_input": {"plan": ""},
            "tool_response": {"filePath": str(plan_file)},
        }
    )

    assert json.loads(answer)["continue"] is False


def test_the_tool_can_carry_nothing_but_the_response(wtx_repo: Path, fake_bin: Path) -> None:
    """The shape a model that follows the 2.1.267 tool description produces:
    ExitPlanMode called with no arguments, the plan only in the result."""
    from wtx import orchestrate

    _enable_orchestration(wtx_repo)
    path = make_worktree(wtx_repo, "feat/bare")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)

    answer = orchestrate.capture(
        {
            "cwd": str(path),
            "session_id": "conv-bare",
            "tool_name": "ExitPlanMode",
            "tool_input": {},
            "tool_response": {"plan": "1. do it\n\nwtx-size: large\n"},
        }
    )

    assert json.loads(answer)["continue"] is False


def test_a_leaked_wt_branch_does_not_rename_the_session(
    wtx_repo: Path, fake_bin: Path, monkeypatch
) -> None:
    """An agent launched from another worktree's shell carries that worktree's
    WT_BRANCH. Trusting it names a session that does not exist, and the handoff
    is then recorded for nobody."""
    from wtx import notify

    path = make_worktree(wtx_repo, "feat/mine")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)
    ctx = context.load(root=path)
    monkeypatch.setenv("WT_BRANCH", "feat/somewhere-else")

    assert notify.session_for(path) == ctx.session


def test_a_handoff_that_does_nothing_says_why(wtx_repo: Path, fake_bin: Path) -> None:
    """Every guard in capture() looks the same from outside, the agent just
    carries on. The log is the only way to tell which one fired."""
    from wtx import orchestrate

    path = make_worktree(wtx_repo, "feat/quiet")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)

    assert _accept(path, "1. do it\n\nwtx-size: large\n") == ""

    log = orchestrate.log_file().read_text()
    assert "orchestration off" in log


def test_the_log_records_a_handoff_end_to_end(
    wtx_repo: Path, fake_bin: Path, no_fork: list
) -> None:
    from wtx import orchestrate

    _enable_orchestration(wtx_repo, build_model='"opus"')
    path = make_worktree(wtx_repo, "feat/logged")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)
    ctx = context.load(root=path)

    _accept(path, "1. do it\n\nwtx-size: large\n", conversation="conv-log")
    orchestrate.run(no_fork[0])

    log = orchestrate.log_file().read_text()
    assert "record written" in log
    assert "claude -r conv-log" in log
    assert f"respawned {ctx.session}" in log


def test_an_agent_outside_a_session_is_never_stopped(
    wtx_repo: Path, fake_bin: Path, monkeypatch
) -> None:
    """continue: false with no pane to respawn would leave a human holding a
    plan and no way to start on it."""
    from wtx import tmux

    _enable_orchestration(wtx_repo)
    path = make_worktree(wtx_repo, "figure/no-session")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    monkeypatch.setattr(tmux, "has_session", lambda name: False)

    assert _accept(path, "1. do it\n\nwtx-size: large\n") == ""


def test_a_hook_finds_the_session_of_a_repo_that_renamed_itself(
    wtx_repo: Path, fake_bin: Path
) -> None:
    """The session is named from [repo].name. A hook guessing from the origin
    URL instead would record a handoff nothing ever drains."""
    from wtx import notify

    text = (wtx_repo / "wtx.toml").read_text().replace('name = "remote"', 'name = "renamed-app"', 1)
    (wtx_repo / "wtx.toml").write_text(text)
    path = make_worktree(wtx_repo, "feat/renamed")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)
    ctx = context.load(root=path)

    assert ctx.session == "renamed-app/feat/renamed"
    assert notify.session_for(path) == ctx.session


def test_a_pending_handoff_shows_in_status(
    wtx_repo: Path, fake_bin: Path, monkeypatch, capsys
) -> None:
    """A handoff that never fires must not be invisible."""
    _enable_orchestration(wtx_repo, build_model='"opus"')
    path = make_worktree(wtx_repo, "feat/visible")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)

    _accept(path, "1. do it\n\nwtx-size: large\n")
    monkeypatch.chdir(wtx_repo)
    capsys.readouterr()
    run(["status", "feat/visible"])
    assert "handoff pending: large plan -> opus" in capsys.readouterr().out


def test_plan_effort_survives_a_later_setup(wtx_repo: Path, fake_bin: Path, monkeypatch) -> None:
    """A flag passed once to `wtx go` has nowhere to live but .env.worktree.
    The handoff and every later setup rebuild the context from scratch."""
    _enable_orchestration(wtx_repo, plan_model='"fable"')
    make_worktree(wtx_repo, "feat/big-plan")
    monkeypatch.chdir(wtx_repo)
    run(
        [
            "go",
            "feat/big-plan",
            "--prompt",
            "Rewrite the ports module.",
            "--plan-effort",
            "high",
            "--plan-model",
            "opus",
            "--no-attach",
        ]
    )
    path = wtx_repo / ".claude" / "worktrees" / "feat-big-plan"
    values = envfile.read_worktree(path)
    assert values["WTX_PLAN_EFFORT"] == "high"
    assert values["WTX_PLAN_MODEL"] == "opus"

    ctx = context.load(root=path)
    assert ctx.plan_effort == "high"
    assert ctx.plan_model == "opus"

    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    assert envfile.read_worktree(path)["WTX_PLAN_EFFORT"] == "high"


def test_a_briefed_session_plans_at_the_plan_effort(
    wtx_repo: Path, fake_bin: Path, monkeypatch
) -> None:
    """Planning is reading and thinking. Low unless the caller said otherwise."""
    _enable_orchestration(wtx_repo, plan_model='"fable"', plan_effort='"low"')
    path = make_worktree(wtx_repo, "feat/plan-effort")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)

    monkeypatch.chdir(wtx_repo)
    run(["brief", "feat/plan-effort", "--prompt", "Add a health endpoint."])
    sent = [c for c in calls_of(fake_bin, "tmux") if "send-keys" in c]
    assert any("--model fable --permission-mode plan --effort low" in c for c in sent)


def test_the_plan_names_the_effort_the_build_runs_at(
    wtx_repo: Path, fake_bin: Path, no_fork: list
) -> None:
    """The planner has just read the code. It knows better than small/large."""
    from wtx import orchestrate

    _enable_orchestration(
        wtx_repo, plan_model='"fable"', build_model='"opus"', large_effort='"xhigh"'
    )
    path = make_worktree(wtx_repo, "feat/named-effort")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)

    answer = json.loads(
        _accept(path, "1. rename it\n\nwtx-effort: medium\n", conversation="conv-e")
    )
    assert "medium" in answer["stopReason"]
    assert json.loads(no_fork[0].read_text())["effort"] == "medium"

    orchestrate.run(no_fork[0])
    sent = [c for c in calls_of(fake_bin, "tmux") if "send-keys" in c]
    assert any("claude -r conv-e --model opus" in c and "--effort medium" in c for c in sent)


def test_a_plan_that_still_says_small_gets_the_small_effort(
    wtx_repo: Path, fake_bin: Path, no_fork: list
) -> None:
    """Plans written before the wtx-effort marker existed must keep working."""
    from wtx import orchestrate

    _enable_orchestration(wtx_repo, build_model='"opus"', small_effort='"medium"')
    path = make_worktree(wtx_repo, "feat/old-marker")
    setup_mod.run_setup(context.load(root=path), start_tmux=True)

    _accept(path, "1. rename it\n\nwtx-size: small\n")
    assert json.loads(no_fork[0].read_text())["effort"] == "medium"
    orchestrate.run(no_fork[0])
    sent = [c for c in calls_of(fake_bin, "tmux") if "send-keys" in c]
    assert any("--model opus" in c and "--effort medium" in c for c in sent)


def test_go_says_when_llm_is_not_what_a_brief_will_use(
    wtx_repo: Path, fake_bin: Path, monkeypatch, capsys
) -> None:
    """--llm names the model for a plain session; a brief takes its models from
    the orchestration block. Silently ignoring the flag is worse than saying so."""
    _enable_orchestration(wtx_repo, plan_model='"fable"', build_model='"opus"')
    make_worktree(wtx_repo, "feat/llm-note")
    monkeypatch.chdir(wtx_repo)
    capsys.readouterr()
    run(["go", "feat/llm-note", "--llm", "haiku", "--prompt", "Do a thing.", "--no-attach"])
    assert "plans on fable (--plan-model) and implements on opus" in capsys.readouterr().err


# -- the servers the agent cannot see -----------------------------------------


def test_every_file_wtx_generates_is_ignored(wtx_repo: Path, fake_bin: Path) -> None:
    """A generated file with no ignore line leaves the worktree permanently
    dirty, and `wtx land` then refuses to land it."""
    path = make_worktree(wtx_repo, "feat/clean")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)

    untracked = git_out(["status", "--porcelain", "--untracked-files=all"], path)
    assert untracked == "", f"setup left the worktree dirty:\n{untracked}"


def test_setup_tells_the_agent_its_ports_and_its_logs(wtx_repo: Path, fake_bin: Path) -> None:
    path = make_worktree(wtx_repo, "feat/note")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    ctx = context.load(root=path)

    note = (path / ".claude" / "rules" / "wtx.md").read_text()
    assert f"http://127.0.0.1:{ctx.port('backend')}/" in note
    assert ".wt-logs/backend.log" in note


def test_wtx_curl_reaches_this_worktrees_port(wtx_repo: Path, fake_bin: Path, monkeypatch) -> None:
    """The agent never has to know the number, and cannot aim this anywhere
    but its own checkout."""
    path = make_worktree(wtx_repo, "feat/curl")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    ctx = context.load(root=path)
    monkeypatch.chdir(path)

    assert run(["curl", "backend", "/api/health", "-X", "POST"]) == 0
    called = calls_of(fake_bin, "curl")[-1]
    assert called == f"-X POST http://127.0.0.1:{ctx.port('backend')}/api/health"


def test_wtx_curl_names_the_families_it_knows(
    wtx_repo: Path, fake_bin: Path, monkeypatch, capsys
) -> None:
    path = make_worktree(wtx_repo, "feat/curl-bad")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)
    monkeypatch.chdir(path)

    assert run(["curl", "nope", "/"]) == 1
    assert "backend" in capsys.readouterr().err


def test_both_agent_facing_documents_name_wtx_curl(wtx_repo: Path, fake_bin: Path) -> None:
    """It is the only way an agent reaches these servers, and nothing about it
    is guessable: not the command, not the port. Both files it reads say so."""
    path = make_worktree(wtx_repo, "feat/documented")
    setup_mod.run_setup(context.load(root=path), start_tmux=False)

    note = (path / ".claude" / "rules" / "wtx.md").read_text()
    assert "wtx curl" in note
    assert "cannot see or" in note  # and why plain curl is not enough

    # The repo's own CLAUDE.md is a human's file: wtx prints this section for
    # someone to paste rather than writing it, so only its text is checked.
    answers = init_mod.scan(wtx_repo)
    assert "wtx curl" in init_mod.claude_section(answers)
