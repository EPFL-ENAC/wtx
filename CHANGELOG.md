# Changelog

## unreleased

New:

- `[agent.orchestration]`: plan on one model, implement on another. A brief
  starts the agent on `plan_model` in plan mode; accepting the plan carries the
  same conversation on to `build_model` with `claude -r`, at the effort the
  plan's size asks for. Works with opencode too: opencode.json gets a model per
  agent under `agent`, so the TUI swaps the model with the agent when the plan
  is accepted (no `wtx-effort:` routing there). Off unless a repo turns it on.
  Claude needs the new `ExitPlanMode` hook, so re-run
  `wtx install-machine --apply`.
- `wtx init --edit` re-runs the setup on a repo that already has wtx: it merges
  `wtx.toml` with a fresh scan and prints it, with a `_changes` block saying
  where the repo moved on since the file was written. The file's own values
  win, nothing is written. `/wtx-init` starts there now.
- `wtx handoff`, the command that hook calls. A pending handoff shows in
  `wtx status`.
- `wtx curl <family> [path] [curl args]` reaches one of this worktree's own
  servers without knowing its port. The baseline allows it with any flags and
  any method, which the plain `curl` rules cannot do: an ask rule beats every
  allow rule, so `curl -X POST` at your own backend prompts.
- Setup writes `.claude/rules/wtx.md`, loaded every session: the worktree's
  ports, its server log files, and `wtx curl`. The agent could not work any of
  it out from the repository. `wtx init` now names `wtx curl` in the CLAUDE.md
  section it writes, where it used to say only that curl to localhost was
  allowed.
- Permission modes and effort levels in `wtx.toml` are checked against the ones
  the agent actually knows.

Review fixes:

- `{repo}` in a `[[repos]]` path was expanded to nothing when `[repo].name`
  was missing, so the k8s entry pointed at the whole lab folder. The default
  name now comes from the origin URL before any expansion.
- `wtx go` run from inside another worktree's session handed that worktree's
  `WTX_LLM` and `WTX_AGENT` to the new one. The hook now reads `WTX_GO_*`
  names no pane exports, and they are scrubbed from the tmux server.
- `wtx land` refused any branch whose base had moved on, so its rebase never
  did anything. It now rebases, aborts on conflicts with a message, and only
  refuses a branch cut from a newer protected branch.
- `--with <name>` alone pairs on a branch named like the app branch, as
  planned. It was a no-op.
- A `SetupError`, `CommandError` or `WtError` printed a traceback instead of
  a message.
- Claude sessions outside any wtx session got no desktop notification at all.
  They get one again, titled with the directory, and one click wakes one
  worker.
- `.env.worktree` always carries the "yours" marker, so a key added under it
  survives the next run and the header is true.
- `min_wtx_version` is checked. Shell completion for `--with` lists only the
  `[[repos]]` names. `python -m wtx` works. The tests no longer need `wt`
  installed.

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
