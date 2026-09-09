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
- An `ask` rule beats everything. No bare interpreters in it.
- Setup is idempotent. wt fires its hooks more than once.

## Layout

- `src/wtx/` the package. `config` and `context` are read by everything else.
- `src/wtx/agents/` one module per coding agent, behind the protocol in `base.py`.
- `src/wtx/templates/` the permission baselines, shipped as package data.
- `src/wtx/skills/wtx-init/` the guided setup skill, installed by `install-machine`.
- `tests/` real temp git repos, with wt, tmux and the agents faked. A test must
  never touch the developer's tmux server and never push anywhere real.

## Working here

```sh
uv venv && uv pip install -e . pytest ruff
python -m pytest tests/ -q
ruff check src/ tests/
```

Tests that need a real tmux server are marked `tmux` and skipped by default.
