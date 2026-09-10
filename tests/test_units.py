"""Unit tests for the parts with no side effects."""

from __future__ import annotations

import fnmatch
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from wtx import config, envfile, guard, orchestrate
from wtx.agents.base import RenderContext
from wtx.agents.claude import ClaudeAgent, protected_branch_rules
from wtx.config import parse, validate
from wtx.context import session_name
from wtx.ports import cksum
from wtx.repos import parse_with
from wtx.tmux import _split_percent

# -- ports -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["", "a", "speed-to-zero/feat/x", "resslab-hub/dev", "x" * 300]
)
def test_cksum_matches_the_posix_tool(text: str) -> None:
    """Ports must land where the bash version put them, or every live worktree
    moves the first time it is set up again."""
    real = subprocess.run(
        ["cksum"], input=text.encode(), capture_output=True, check=True
    ).stdout.split()[0]
    assert cksum(text.encode()) == int(real)


def test_ports_reproduce_a_live_worktree() -> None:
    """Two ports taken from a real checkout before wtx existed."""
    assert 18000 + cksum(b"leure-speed-to-zero/feat/tcaf-deployment") % 500 == 18065
    assert (
        18000 + cksum(b"leure-speed-to-zero/feat/add-negawatt-database-levers") % 500
        == 18387
    )


# -- session names -----------------------------------------------------------


def test_session_name_drops_characters_tmux_refuses() -> None:
    assert session_name("repo", "feat/x") == "repo/feat/x"
    assert session_name("repo", "release-1.2") == "repo/release-1-2"
    assert session_name("repo", "a:b") == "repo/a-b"


def test_split_percent_matches_the_old_layout() -> None:
    assert [50, _split_percent(2, 3), _split_percent(3, 3)] == [50, 67, 50]


# -- env file ----------------------------------------------------------------


def test_write_keeps_keys_wtx_does_not_own(tmp_path) -> None:
    """The bug that motivated the rewrite: a hand-added key was dropped on
    every setup run, including the automatic one."""
    f = tmp_path / ".env.worktree"
    f.write_text("WT_BRANCH=old\nBACKEND_PORT=18000\n# mine\nMODEL_PROFILE=tcaf\n")
    envfile.write(
        f,
        {"WT_BRANCH": "new", "BACKEND_PORT": "18001"},
        ["WT_BRANCH", "BACKEND_PORT"],
    )
    values = envfile.read(f)
    assert values["WT_BRANCH"] == "new"
    assert values["MODEL_PROFILE"] == "tcaf"
    assert "# mine" in f.read_text()


def test_write_is_stable_across_runs(tmp_path) -> None:
    f = tmp_path / ".env.worktree"
    owned = {"WT_BRANCH": "b", "BACKEND_PORT": "1"}
    keys = ["WT_BRANCH", "BACKEND_PORT"]
    envfile.write(f, owned, keys)
    f.write_text(f.read_text() + "EXTRA=1\n")
    envfile.write(f, owned, keys)
    settled = f.read_text()
    envfile.write(f, owned, keys)
    envfile.write(f, owned, keys)
    assert f.read_text() == settled
    assert envfile.read(f)["EXTRA"] == "1"


def test_values_with_spaces_survive(tmp_path) -> None:
    f = tmp_path / ".env.worktree"
    envfile.write(f, {"WT_BRANCH": "a b/c"}, ["WT_BRANCH"])
    assert envfile.read(f)["WT_BRANCH"] == "a b/c"


def test_load_snippet_unsets_when_there_is_no_file() -> None:
    snippet = envfile.load_snippet(["A", "B"])
    assert "unset A B" in snippet
    assert snippet.startswith("set -a")


# -- config ------------------------------------------------------------------


def test_uv_no_sync_is_added_when_a_repo_installs_editable() -> None:
    cfg = parse(
        {
            "repos": [
                {
                    "name": "m",
                    "path": "../m",
                    "access": "pair",
                    "editable_install": {"run": "uv pip install -e {path}"},
                }
            ]
        }
    )
    assert cfg.env_extra["UV_NO_SYNC"] == "1"
    assert cfg.needs_uv_no_sync


def test_lab_and_repo_are_expanded_in_paths() -> None:
    cfg = parse(
        {
            "repo": {"name": "speed-to-zero", "lab": "leure"},
            "repos": [{"name": "k8s", "path": "~/k/epfl-{lab}/{repo}"}],
        }
    )
    assert cfg.repos[0].path == "~/k/epfl-leure/speed-to-zero"


def test_validate_catches_a_base_branch_left_unprotected() -> None:
    cfg = parse({"repo": {"base_branch": "dev", "protected_branches": ["main"]}})
    assert any("base branch" in p for p in validate(cfg))


def test_validate_catches_a_missing_agent_pane() -> None:
    cfg = parse({"panes": {"pane": [{"name": "shell", "role": "shell"}]}})
    assert any("role = \"agent\"" in p for p in validate(cfg))


def test_validate_catches_two_families_on_one_env_key() -> None:
    cfg = parse(
        {"ports": {"family": [{"name": "a", "main": 1, "env": "P"}, {"name": "b", "main": 2, "env": "P"}]}}
    )
    assert any("same env key" in p for p in validate(cfg))


def test_env_prefix_names_the_keys() -> None:
    cfg = parse({"repos": [{"name": "model", "path": "../m", "env_prefix": "TCM"}]})
    assert cfg.repos[0].path_key == "TCM_PATH"
    assert cfg.repos[0].branch_key == "TCM_BRANCH"
    plain = parse({"repos": [{"name": "my-repo", "path": "../m"}]})
    assert plain.repos[0].path_key == "WTX_REPO_MY_REPO_PATH"


# -- permissions -------------------------------------------------------------


def test_protected_branch_rules_cover_both_push_spellings() -> None:
    rules = protected_branch_rules(("dev",))
    assert "Bash(git push * dev)" in rules
    assert "Bash(git push *:dev)" in rules


def test_baseline_has_no_bare_interpreter_in_ask() -> None:
    """An ask rule beats everything, even auto mode. A bare interpreter there
    prompts on every heredoc edit, which is how an agent writes files."""
    from wtx.agents.claude import _baseline

    ask = _baseline()["permissions"]["ask"]
    for bad in ("Bash(python -)", "Bash(python3 -)", "Bash(node -)", "Bash(perl -)"):
        assert bad not in ask


def test_a_repo_cannot_remove_a_baseline_rule() -> None:
    """wtx.toml appends. If it could subtract, one typo reopens force pushes."""
    from wtx.agents.base import RenderContext

    cfg = parse(
        {
            "repo": {"base_branch": "dev", "protected_branches": ["dev"]},
            "permissions": {"allow": ["Bash(git push * --force*)"]},
        }
    )
    ctx = RenderContext(
        root=__import__("pathlib").Path("/tmp/x"),
        main=__import__("pathlib").Path("/tmp/x"),
        branch="b",
        session="s",
        cfg=cfg,
    )
    settings = ClaudeAgent().build_settings(ctx)
    assert "Bash(git push * --force*)" in settings["permissions"]["deny"]


# -- guard -------------------------------------------------------------------


def test_guard_hook_is_valid_shell_and_names_the_branches(tmp_path) -> None:
    cfg = parse({"repo": {"base_branch": "dev", "protected_branches": ["dev", "main"]}})
    body = guard.render_hook(cfg)
    f = tmp_path / "pre-push"
    f.write_text(body)
    subprocess.run(["sh", "-n", str(f)], check=True)
    assert "dev main" in body
    assert guard.MARKER in body


def test_guard_does_not_read_a_file_from_the_repo() -> None:
    """The old guard sourced a script from the checkout. A branch cut before it
    existed had no guard at all."""
    cfg = parse({"repo": {"protected_branches": ["main"]}})
    assert "scripts/" not in guard.render_hook(cfg)


# -- with ---------------------------------------------------------------------


def test_parse_with_separates_access_from_pairing() -> None:
    assert parse_with(["k8s"]) == {"k8s": ""}
    assert parse_with(["model=feat/x"]) == {"model": "feat/x"}
    assert parse_with(["a", "b=c"]) == {"a": "", "b": "c"}


def test_no_module_reads_wt_main() -> None:
    """wt calls "main" whichever worktree holds the default branch, so the
    variable it exports lies. Everything resolves through git-common-dir."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "wtx"
    reads = re.compile(r"""environ(?:\.get\(|\[)\s*['"]WT_MAIN['"]""")
    hits = [f.name for f in src.rglob("*.py") if reads.search(f.read_text())]
    assert hits == [], f"WT_MAIN is read in {hits}"


def test_repo_placeholder_survives_a_missing_name(tmp_path) -> None:
    """With no [repo].name, {repo} was expanded to nothing and the k8s path
    became the whole lab folder."""
    d = tmp_path / "speed-to-zero"
    d.mkdir()
    (d / "wtx.toml").write_text(
        '[repo]\nlab = "leure"\n[[repos]]\nname = "k8s"\npath = "~/k/epfl-{lab}/{repo}"\n'
    )
    cfg = config.load(d / "wtx.toml")
    assert cfg.repo.name == "speed-to-zero"
    assert cfg.repos[0].path == "~/k/epfl-leure/speed-to-zero"


def test_min_wtx_version_refuses_an_old_wtx() -> None:
    assert not validate(parse({"repo": {"name": "x"}, "min_wtx_version": "0.0.1"}))
    problems = validate(parse({"repo": {"name": "x"}, "min_wtx_version": "999.0"}))
    assert any("uv tool upgrade" in p for p in problems)


def test_marker_is_always_written_and_a_key_under_it_survives(tmp_path) -> None:
    f = tmp_path / ".env.worktree"
    owned = {"WT_BRANCH": "b"}
    envfile.write(f, owned, ["WT_BRANCH"])
    assert envfile.FOREIGN_MARKER in f.read_text()
    f.write_text(f.read_text() + "MINE=1\n")
    envfile.write(f, owned, ["WT_BRANCH"])
    assert envfile.read(f)["MINE"] == "1"


def test_install_machine_replaces_the_old_notify_hooks_and_keeps_others(tmp_path) -> None:
    from wtx import machine

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {
        "Stop": [
            {"matcher": "", "hooks": [{"type": "command", "command": "/x/persist.sh"}]},
            {"matcher": "*", "hooks": [{"type": "command", "command": "/x/claude-notify stop"}]},
        ],
        "Notification": [
            {"matcher": "permission_prompt",
             "hooks": [{"type": "command", "command": "/x/claude-notify permission"}]},
        ],
    }}))
    fragment = json.loads(machine.planned("claude")[-1].body)["hooks"]
    assert machine._merge_hooks(settings, fragment)
    hooks = json.loads(settings.read_text())["hooks"]
    text = json.dumps(hooks)
    assert "claude-notify" not in text
    assert "/x/persist.sh" in text
    assert sum(1 for e in hooks["Stop"] if e["matcher"] == "*") == 1
    assert machine._merge_hooks(settings, fragment) is False  # second run: nothing to do


def test_install_machine_drops_an_old_hook_sitting_next_to_ours(tmp_path) -> None:
    """The first wtx build appended its hooks next to claude-notify's. A later
    run must still remove the old ones, even though ours are already there."""
    from wtx import machine

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {
        "Stop": [
            {"matcher": "*", "hooks": [{"type": "command", "command": "/x/claude-notify stop"}]},
            {"matcher": "*", "hooks": [{"type": "command", "command": "wtx notify stop"}]},
        ],
    }}))
    fragment = json.loads(machine.planned("claude")[-1].body)["hooks"]
    assert machine._merge_hooks(settings, fragment)
    text = json.dumps(json.loads(settings.read_text()))
    assert "claude-notify" not in text
    assert text.count("wtx notify stop") == 1


def test_install_machine_comments_out_the_old_bashrc_line(tmp_path) -> None:
    from wtx import machine

    rc = tmp_path / ".bashrc"
    rc.write_text("export A=1\nsource ~/code/resslab-hub/scripts/wt-go.bash\n")
    change = machine.Change(rc, "shell", f"\n# wtx\n{machine.BASHRC_LINE}\n", stale=machine._is_old_bashrc)
    assert change.needed()
    assert change.comment_out_stale() == 1
    text = rc.read_text()
    assert "# replaced by wtx: source ~/code/resslab-hub/scripts/wt-go.bash" in text
    assert "export A=1" in text


# -- orchestration -----------------------------------------------------------


def _orchestrated(**over) -> config.WtxConfig:
    orch = {"enabled": True, **over}
    return parse(
        {
            "repo": {"name": "app", "base_branch": "dev", "protected_branches": ["dev"]},
            "agent": {"llm": "opus", "orchestration": orch},
        }
    )


def _rctx(cfg: config.WtxConfig) -> RenderContext:
    return RenderContext(
        root=Path("/w"), main=Path("/m"), branch="feat/x", session="app/feat-x", cfg=cfg
    )


@pytest.mark.parametrize(
    "line",
    ["wtx-size: small", "- wtx-size: small", "**wtx-size:** small", "WTX-SIZE: Small"],
)
def test_the_planner_sizes_its_own_plan(line: str) -> None:
    assert orchestrate.size_of(f"step one\nstep two\n\n{line}\n") == "small"


def test_a_plan_with_no_marker_is_large() -> None:
    """Guessing from the length is a guess, and guessing low costs quality
    where guessing high only costs money."""
    assert orchestrate.size_of("one two three") == "large"


def test_the_marker_is_only_read_from_a_line_of_its_own() -> None:
    """A plan that quotes the instruction must not be read as sizing itself."""
    assert orchestrate.size_of("I will end with the wtx-size: small line.") == "large"


def test_the_size_routes_effort_and_leaves_the_model_alone() -> None:
    """Anthropic's guidance: effort is usually the better lever, so a smaller
    model stays opt-in."""
    cfg = _orchestrated(build_model="opus", small_effort="medium", large_effort="xhigh")
    assert orchestrate.model_for(cfg, "small") == "opus"
    assert orchestrate.model_for(cfg, "large") == "opus"

    def effort(size: str) -> str:
        return replace(_rctx(cfg), phase="build", size=size, llm="opus").effort

    assert effort("small") == "medium"
    assert effort("large") == "xhigh"


def test_a_repo_can_still_opt_into_a_smaller_model() -> None:
    cfg = _orchestrated(small_model="sonnet")
    assert orchestrate.model_for(cfg, "small") == "sonnet"
    assert orchestrate.model_for(cfg, "large") == "opus"


def test_a_plan_brief_runs_on_the_planning_model_the_settings_do_not() -> None:
    """The worktree keeps its own model. Only the plan phase is redirected, or
    a later `claude --continue` would come back on the planner."""
    ctx = _rctx(_orchestrated(plan_model="fable"))
    assert ctx.model == "opus"
    assert replace(ctx, phase="plan").model == "fable"
    assert replace(ctx, phase="build", llm="sonnet").model == "sonnet"
    assert ClaudeAgent().build_settings(ctx)["model"] == "opus"


def test_a_build_starts_in_auto_mode_by_default() -> None:
    """The human has just read the plan and said yes. Asking again about every
    edit and command hands them back a job they thought they were done with."""
    build = replace(_rctx(_orchestrated()), phase="build", llm="opus", size="large")
    assert build.permission_mode == "auto"
    assert "--permission-mode auto" in ClaudeAgent().handoff_cmd(
        build, session="abc-123", prompt="go"
    )


def test_each_phase_starts_in_its_own_permission_mode() -> None:
    base = _rctx(_orchestrated(build_permission_mode="acceptEdits"))  # not the default
    agent = ClaudeAgent()
    plan = agent.launch_cmd(replace(base, phase="plan"), brief=True)
    build = agent.handoff_cmd(
        replace(base, phase="build", llm="opus", size="large"),
        session="abc-123",
        prompt="go",
    )
    assert "--model fable --permission-mode plan" in plan
    assert "--model opus --permission-mode acceptEdits --effort xhigh" in build


def test_the_handoff_resumes_the_planning_conversation() -> None:
    """Not a fresh one: everything the planner read is most of what the
    implementation needs, and Claude Code's own opusplan switches this way."""
    cmd = ClaudeAgent().handoff_cmd(
        replace(_rctx(_orchestrated()), phase="build", llm="opus", size="large"),
        session="abc 123",
        prompt="Implement it.",
    )
    assert cmd.startswith("claude -r 'abc 123' ")
    assert cmd.endswith("'Implement it.'")


def test_opencode_cannot_be_handed_a_conversation() -> None:
    from wtx.agents.opencode import OpencodeAgent

    ctx = _rctx(_orchestrated())
    assert OpencodeAgent().handoff_cmd(ctx, session="x", prompt="go") == ""


def test_the_accepted_plan_hook_is_part_of_the_machine_setup() -> None:
    """Without it nothing ever hands a plan over."""
    fragment = ClaudeAgent().hook_fragment()
    entry = fragment["PostToolUse"][0]
    assert entry["matcher"] == "ExitPlanMode"
    assert entry["hooks"][0]["command"] == "wtx handoff"


def test_validate_catches_a_permission_mode_that_does_not_exist() -> None:
    cfg = parse({"agent": {"brief_permission_mode": "planning"}})
    assert any("brief_permission_mode" in e for e in validate(cfg))


def test_validate_catches_orchestration_with_nothing_to_hand_to() -> None:
    cfg = _orchestrated(build_model="")
    assert any("orchestration" in e for e in validate(cfg))


def test_validate_catches_an_effort_level_that_does_not_exist() -> None:
    cfg = _orchestrated(large_effort="maximum")
    assert any("large_effort" in e for e in validate(cfg))
    assert any("effort" in e for e in validate(parse({"agent": {"effort": "huge"}})))


def test_orchestration_needs_the_agent_that_has_the_hook() -> None:
    """opencode has no ExitPlanMode hook, so nothing would ever fire."""
    cfg = _orchestrated()
    assert validate(cfg) == []
    cfg = parse(
        {
            "repo": {"name": "app", "base_branch": "dev", "protected_branches": ["dev"]},
            "agent": {"tool": "opencode", "orchestration": {"enabled": True}},
        }
    )
    assert any("orchestration" in e for e in validate(cfg))


# -- reaching the worktree's own servers --------------------------------------


def _served() -> config.WtxConfig:
    return parse(
        {
            "repo": {"name": "app", "base_branch": "dev", "protected_branches": ["dev"]},
            "ports": {"family": [{"name": "backend", "main": 8000}]},
            "panes": {
                "pane": [
                    {"name": "agent", "role": "agent"},
                    {"name": "backend", "role": "server", "cmd": "run", "log": True},
                ]
            },
        }
    )


def _bash_rules(rules: list[str]) -> list[str]:
    return [r[len("Bash(") : -1] for r in rules if r.startswith("Bash(")]


def _matches(command: str, rules: list[str]) -> bool:
    """A rule is a glob over the whole command text, `*` matching any run of
    characters including none."""
    return any(fnmatch.fnmatchcase(command, rule) for rule in rules)


def test_the_agent_may_curl_its_own_server_with_any_method() -> None:
    """An ask rule beats every allow rule, so `curl -X POST` at the worktree's
    own backend prompts however the allow list is written. `wtx curl` builds
    the URL itself and matches no ask rule, whatever flags follow."""
    perms = ClaudeAgent().build_settings(_rctx(_served()))["permissions"]
    command = "wtx curl backend /items -X POST -d @body.json"
    assert _matches(command, _bash_rules(perms["allow"]))
    assert not _matches(command, _bash_rules(perms["ask"]))


def test_a_read_goes_anywhere_and_a_write_prompts() -> None:
    """Reading the docs is the agent's own business, writing is not.

    `*` matches the empty string, so a rule that names a flag is written
    `*-d *`. Written `* -d *` it needs a literal space before the flag and
    misses `curl -d body url`, where the flag comes first and nothing
    precedes it.
    """
    perms = ClaudeAgent().build_settings(_rctx(_served()))["permissions"]
    allow, ask = _bash_rules(perms["allow"]), _bash_rules(perms["ask"])
    for read in ("curl https://docs.rs/tokio", "curl -sSL https://example.com/x"):
        assert _matches(read, allow), read
        assert not _matches(read, ask), read
    for write in (
        "curl -d @body.json https://example.com/api",
        "curl -X POST https://example.com/api",
        "curl --json '{}' https://example.com/api",
        "curl -T f.txt https://example.com/up",
        "curl -F f=@x https://example.com/up",
        "gh api -X POST /repos/o/r/issues",
        "gh api -f title=x /repos/o/r/issues",
        "wget --post-data=x https://example.com/",
    ):
        assert _matches(write, ask), write


def test_wtx_curl_runs_outside_the_sandbox() -> None:
    """Like curl itself: the Linux sandbox cannot reach loopback, so a
    sandboxed wtx curl would never reach the worktree's own servers."""
    settings = ClaudeAgent().build_settings(_rctx(_served()))
    assert "wtx curl *" in settings["sandbox"]["excludedCommands"]


def test_the_worktree_note_names_the_ports_and_the_logs(tmp_path) -> None:
    """The agent cannot work either out: the ports are picked per worktree and
    the servers run in panes it cannot reach."""
    from wtx import envfile
    from wtx.agents.claude import worktree_rules

    envfile.write_worktree(tmp_path, {"BACKEND_PORT": "18042"}, ["BACKEND_PORT"])
    ctx = replace(_rctx(_served()), root=tmp_path)
    text = worktree_rules(ctx)

    assert "http://127.0.0.1:18042/" in text
    assert ".wt-logs/backend.log" in text
    assert "wtx curl backend" in text
    assert "tail -f" in text  # named as the thing not to do


def test_a_repo_with_no_servers_gets_no_note(tmp_path) -> None:
    """Nothing to say is better than a section saying nothing."""
    from wtx.agents.claude import worktree_rules

    ctx = replace(_rctx(parse({"repo": {"name": "app"}})), root=tmp_path)
    assert worktree_rules(ctx) == ""
