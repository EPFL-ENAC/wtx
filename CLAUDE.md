# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# wtx

A Python CLI that gives every git branch its own worktree, ports, tmux session
and coding agent. It replaces a set of bash scripts that were copied by hand
into three repos and then drifted apart.

## Rules that are not negotiable

Read `docs/traps.md` before changing anything. Every rule there is the result of
something going wrong once, and most of them have a test.

The short version:

- The main checkout is resolved with `git rev-parse --git-common-dir`. Never
  read `$WT_MAIN`, it points at whichever worktree holds the default branch.
- The push guard is a plain `.git/hooks/pre-push` with everything baked in.
  Never a hook manager job, never reading a file from the repo.
- `.env.worktree` is merged, never rewritten. Keys wtx does not own must survive.
- Call `proc.is_dry_run()`. Never `from .proc import DRY_RUN`, that copies the
  value and a dry run then changes things for real.
- `wtx.toml` can only add to the permission baseline, never remove from it.
- An `ask` rule beats everything. No bare interpreters in it. A rule that
  names a flag is written `*-d *`: `* -d *` misses the flag in first place.
- Setup is idempotent. wt fires its hooks more than once.
- A hook never restarts its own pane. It records what it wants and a detached
  process does it, or the restart kills the hook halfway through.
- Every file wtx generates in a checkout is in both ignore lists. An untracked
  generated file makes the worktree dirty and `wtx land` refuses it.
- Setup reads everything (config, templates, seeds) from the main checkout. A
  feature branch must not be able to change the rules its agent runs under.

## Working here

```sh
uv venv && uv pip install -e . pytest ruff
python -m pytest tests/ -q                                  # all tests
python -m pytest tests/test_units.py -k guard -q            # one test by name
python -m pytest tests/test_real_tmux.py --run-tmux -q      # needs a real tmux server
ruff check src/ tests/
ruff format src/ tests/                                     # the repo is formatted
wtx --dry-run go feat/x                                     # print, do not run
```

Tests that need a real tmux server are marked `tmux` and skipped unless
`--run-tmux` is passed.

No runtime dependencies, stdlib only (Python 3.11+). `tomllib` reads config.

## Layout

- `src/wtx/` the package. `config` and `context` are read by everything else.
- `src/wtx/agents/` one module per coding agent, behind the protocol in `base.py`.
- `src/wtx/orchestrate.py` the plan-then-build handoff, driven by the agent's
  ExitPlanMode hook. It resumes the planning conversation, never re-briefs.
- `src/wtx/templates/` the permission baselines, shipped as package data.
- `src/wtx/skills/wtx-init/` the guided setup skill, installed by `install-machine`.
- `src/wtx/shell/wtx.bash` the `wtgo` / `wtdone` shell functions, printed by
  `wtx shell-init`.
- `tests/` real temp git repos, with wt, tmux and the agents faked. A test must
  never touch the developer's tmux server and never push anywhere real.

## How a command flows

- `cli.py` parses, then builds one `context.Ctx` per command: main checkout,
  worktree root, branch, config, and the values of `.env.worktree`.
- `wtx go` calls `wt`, which runs the hooks listed in the consumer repo's
  `.wt.toml`. Those call `wtx hook post-create` and friends (`hooks.py`), which
  run `setup.run_setup`: ports, `.env.worktree`, deps, push guard, agent
  settings. Then `tmux.ensure_session` builds the panes from `[panes]`.
- The agent backend (`agents/claude.py`, `agents/opencode.py`) renders the
  per-worktree settings and gives the shell line the agent pane runs.
  `RenderContext` in `base.py` decides model, effort and permission mode,
  including the plan/build phase of orchestration.
- `notify.py` keeps one state file per session under `$XDG_RUNTIME_DIR/wtx`,
  read by `wtx tmux-status` and `wtx monitor`. Handoff records live next to
  them with a `.handoff` suffix so they are not mistaken for state.
- All mutating subprocess calls go through `proc.run`, which honours dry run.

## Tests

- `tests/conftest.py` puts recording stubs for `wt`, `tmux`, `claude`, `npm`,
  `uv`, `gh`... first on PATH. Every call lands in a log; read it with
  `calls_of(fake_bin, "tmux")`. The fake `wt` does real `git worktree` work.
- The `repo` fixture is a temp repo shaped like the real ones (backend,
  frontend, a bare remote, a `dev` base branch).
- When a trap is fixed, add a test that would catch it again. That is how the
  rules above stay true.

## Style

Simple English, short sentences, no em dashes. Comments and docstrings explain
why a thing is done that way, usually by naming the trap it avoids.

## Worktrees (wtx dogfoods itself)

Every branch has its own git worktree, tmux session and agent. The session is
`wtx/<branch>` and its panes are `agent` and `shell`. There are no dev servers
and no ports here.

- Work on this branch only. Never push `main`. When the work is ready, say so
  and a human runs `wtx land <branch>` from the main checkout.
- A pre-push hook enforces this. If it refuses a push, that is the design, not
  a bug to work around.
