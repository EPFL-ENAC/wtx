"""Unit tests for the parts with no side effects."""

from __future__ import annotations

import subprocess

import pytest

from wtx import envfile, guard
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
