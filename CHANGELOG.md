# Changelog

## 0.1.0

First release. Replaces the bash scripts copied into resslab-hub, bluecity-viz
and speed-to-zero.

Added:

- `wtx init`, `go`, `all`, `done`, `land`, `open`, `status`, `setup`,
  `teardown`, `tmux`, `brief`, `hook`, `notify`, `tmux-status`, `monitor`,
  `doctor`, `install-machine`, `shell-init`, `guard`.
- One tracked `wtx.toml` per repo, and a `.wt.toml` that calls `wtx hook`.
- Claude Code and opencode backends, with the model, subagent model, effort,
  context cap and disabled MCP servers set per repo and per worktree.
- External repos: `read` for no-prompt reading with no writes, `pair` for a
  worktree of their own through `--with name=branch`.
- A push guard as a plain git hook, with the protected branches baked in.
- A tmux state per session, a status line segment, and `wtx monitor`.
- The `/wtx-init` skill, installed by `wtx install-machine`.

Fixed, compared with the bash version:

- `.env.worktree` is merged, so a key added by hand or by a repo hook survives.
- A stale worktree entry no longer resolves to the main checkout, which once let
  a push to a protected branch through.
- The guard refuses a `--dry-run` push to a protected branch. The lefthook job
  it replaces was skipped for "no matching files".
- Protected branches are declared once instead of in four files.
