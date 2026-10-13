# What wtx does

A short tour, one section per feature. Every key named here is in
[`schema.md`](schema.md). The last section lists every command.

## A worktree per branch

`wtx go <branch> [base]` makes the branch, its worktree, its tmux session and
its agent, then attaches you to the session. The base is
`origin/<base_branch>` unless you give one, and wtx fetches `origin` first. The
worktree is created by `wt` under `<repo>/.claude/worktrees/<branch>`.

A protected branch is refused: it stays in the main checkout. Running
`wtx go` again on a branch that already has a worktree is safe, setup just runs
again. `--no-attach` skips the attach.

```sh
wtx go feat/thing                # cut from origin/<base_branch>
wtx go fix/bug origin/stage      # cut from another base
wtx all                          # after a reboot: every session back, detached
wtx done feat/thing              # close the worktree, keep the branch
```

`wtx all` (the same as `wtx go all`) brings back the session of every worktree,
detached, then opens the session picker. `wtx done [branch]` removes the
worktree and keeps the branch. It refuses a worktree with uncommitted changes,
`--force` discards them. Run from inside its own session, it hands the job to
the tmux server, because removing the worktree kills the shell that runs it.

`wtx tmux [--no-attach] [--brief]` only creates or attaches the session of the
checkout you are in.

## The tmux session

The session is called `<repo>/<branch>` (a `.` or `:` becomes `-`). It has one
window, `dev`, with the panes listed in `[panes]`. The `agent` pane fills the
left half, the other panes stack on the right.

Every pane exports `.env.worktree`, so a server started there gets its own
ports. A pane with `log = true` is copied to `.wt-logs/<name>.log`. This is how
the agent sees a server: it cannot reach the tmux socket, but it can read a
file.

`prefix+X` opens a small menu on the session you are in: kill the session,
close the worktree and keep the branch, or close it and discard uncommitted
changes.

## Ports

Each worktree gets one offset, from a checksum of `<repo>/<branch>`, and one
port per `[[ports.family]]` at that offset. If a port is taken, the offset
moves on. The ports are written to `.env.worktree` as `<NAME>_PORT`. The main
checkout keeps the `main` port of each family.

Two commands use them, so you never need to know the number:

```sh
wtx open frontend                            # print the URL and open it
wtx open frontend feat/thing --print-only    # another worktree's URL, print only
wtx curl backend /api/health                 # curl this worktree's backend
wtx curl backend /items -X POST -d @body.json
```

For `wtx curl`, the order does not matter. The family can be left out (the
first family is used), and so can the path (`/` is used). `wtx open` also
takes a branch with no family. The agent may run `wtx curl` with any method
and any flags, because it can only reach a port this worktree owns.

## `.env.worktree`

Setup writes `.env.worktree` at the worktree root. It holds `WT_BRANCH`,
`WT_SLUG`, the ports, `WTX_AGENT`, `WTX_LLM`, `WTX_PLAN_MODEL`,
`WTX_PLAN_EFFORT`, the keys from `[env].extra` and `[env].computed`, and the
`<PREFIX>_PATH` and `<PREFIX>_BRANCH` keys of the external repos.

The file is merged, never rewritten. A key wtx does not own survives every
run, so you can add your own under the "yours" marker.

## Setup and teardown

`wtx setup` makes a worktree runnable. The `wt` hooks run it when a worktree is
created, and `wtx go` runs it again each time. You can run it by hand too
(`--path` for another worktree, `--no-tmux` to skip the session). It is safe to
run many times. The steps, in order:

1. Fix the branch tracking, so a plain `git push` goes to its own branch.
2. Seed files from `[seed]`: copied or linked from the main checkout when
   missing, never overwritten.
3. External repos from `[[repos]]`.
4. Write `.env.worktree`.
5. Run the `post_setup` hook (a branch database, a Git LFS pull...).
6. Write the agent settings.
7. Run the dependency steps in `[[deps.step]]`, then `post_install` and the
   editable installs.
8. Install the push guard.
9. Build the tmux session.

Setup reads the config, the templates and the seed files from the main
checkout. A feature branch cannot change the rules its own agent runs under.

`wtx teardown` runs the `pre_teardown` hook, kills the session and frees the
ports. The `wt` pre-remove hook calls it, so `wtx done` and `wtx land` do too.
It never removes the worktree of a paired repo, which may hold work not pushed.

## The push guard

The guard is a plain git `pre-push` hook, in the hooks folder git really uses
(it follows `core.hooksPath`). The protected branches are written inside it.
From a worktree, it refuses a push to a protected branch and a push to any
branch other than the worktree's own. The main checkout is free, it is where
`wtx land` works.

A `pre-push` hook that was there before is kept as `pre-push.before-wt` and
still runs. The guard works next to lefthook. Setup installs it, `wtx guard`
installs it by hand and `wtx guard --show` prints it.

## The agent

`[agent].tool` is `claude` or `opencode`. For one worktree, `wtx go --agent`
picks the tool and `wtx go --llm` the model.

With Claude Code, setup writes `.claude/settings.local.json` (model, subagent
model, effort, compact window, disabled MCP servers, permissions, sandbox) and
`.claude/rules/wtx.md`, which Claude loads every session: the ports, the log
file of each server pane, and how to use `wtx curl`. With
`explore_agent_model` it also writes `.claude/agents/Explore.md`. With
opencode, setup writes `opencode.json`.

The agent pane starts `claude --continue` (opencode: `opencode -c`), so you
find your conversation again. With a brief it starts a new conversation on that
brief (opencode runs the brief with `opencode run`, then opens the TUI on the
same session).

## Permissions

The agent runs on a baseline that wtx ships, plus the `[permissions]` of the
repo. A repo can only add rules, never remove one. In short: reading is
allowed, writing to the internet asks, force pushes and remote changes are
denied, and a push to a protected branch is denied on its own.

An `ask` rule beats every `allow` rule. So never put a bare interpreter
(`python`, `node`, `sh`) in `ask`: the agent writes files through those, and
would ask on every edit.

External repos in `[[repos]]` come in two kinds. `read`: the agent reads it
with no prompt and never writes there. `pair`: the same, until you run
`wtx go <branch> --with <name>[=<branch>]`. Then that repo gets a worktree of
its own, the agent can edit it, and the work lands through that repo's own
pull request.

## Briefs

A brief is a task written down for the agent. `wtx go <branch> --prompt <file
or text>` writes it to `PROMPT.md` at the worktree root. The agent pane renames
it to `PROMPT.sent.md` and starts on it, in `brief_permission_mode` (`plan` by
default). So the agent comes back with a plan, not with edits.

`wtx brief [branch] --prompt <file or text>` sends a brief to a worktree that
already exists.

In Claude Code, the `/wtx-create` skill writes the brief with you and runs
`wtx go` for you.

## Plan on one model, build on another

Turn on `[agent.orchestration]` and a brief plans on `plan_model`, in plan
mode, at `plan_effort`. The plan ends with a line `wtx-effort: <level>`. When
you accept the plan, the `ExitPlanMode` hook runs `wtx handoff`. It restarts
the agent pane on the same conversation (`claude -r`), on `build_model`, at
the effort the plan asked for (`build_effort` when it names none), in
`build_permission_mode`. You read the plan and press accept. Nothing else.

```toml
[agent.orchestration]
enabled = true
plan_model = "fable"     # writes the plan
plan_effort = "low"      # planning is reading and thinking
build_model = "opus"     # implements it, at the effort the plan asked for
```

For one worktree, when you already know the plan is a big one:

```sh
wtx go feat/thing --prompt brief.md --plan-effort high
```

`--plan-model` works the same way. A handoff that is waiting shows in
`wtx status`, and every handoff is traced in `$XDG_RUNTIME_DIR/wtx/handoff.log`.

With opencode there is no restart. When you accept the plan, the TUI switches
to the build agent, and `opencode.json` ties a model to each agent, so the
model follows. The `wtx-effort:` line does nothing there.

## Which session needs you

`wtx install-machine` adds hooks to Claude Code that call `wtx notify` with the
state of the session: `start`, `running`, `stop`, `idle` or `permission`. wtx
keeps one state file per session under `$XDG_RUNTIME_DIR/wtx`.

Inside tmux:

- the status line shows the sessions that wait for you (`wtx tmux-status`).
- `prefix+s` is the session list. A waiting session is coloured: yellow when
  it asks for a permission, cyan when it asks a question.
- `prefix+g` shows every agent pane at once (`wtx monitor --grid`).

Outside tmux, a desktop notification tells you which session needs you. A click
brings the terminal to the front, on that session. `WTX_NOTIFY_CLICK=0` turns
the click off. The banner goes away on its own when the agent starts working
again.

## Monitor

`wtx monitor` opens a board, in a tmux session of its own (`wtx-monitor`):
one line per session, the waiting ones first, with its state, how long ago it
changed, its ports and the last message. Press `1` to `9` to jump to a session,
`r` to refresh, `q` to quit. `--interval` sets how often it redraws.

`wtx monitor --grid` shows up to nine tiles, one per session, each with the
bottom of that session's agent pane. Enter or `o` on a tile goes to that
session, `q` closes the tile.

## Status

`wtx status [branch]` prints one line per worktree: the branch, whether its
session is up, its state, and its ports. Below it: a handoff that is waiting,
and the external repos (paired branch and path, or read only). `--json` gives
the same data plus the session name, the agent and the model.

## Landing

`wtx land [branch]` runs from the main checkout only. It needs `gh`.

1. It refuses a protected branch, a worktree with uncommitted changes, and a
   branch cut from a newer protected branch (for example from `stage` while
   `dev` is behind).
2. It rebases the branch on `origin/<base_branch>`. On a conflict it aborts
   the rebase and tells you where to fix it.
3. It pushes the branch with `--force-with-lease`.
4. It opens a pull request with `gh`, turns on auto-merge (or merges now if the
   repo has no auto-merge), and waits up to 30 minutes for the merge.
5. It removes the worktree and deletes the branch, local and remote.

`--local` skips GitHub. It runs the `[checks]` lint and test commands after
the rebase and before any push, so a branch that fails them never reaches
origin. Then it pushes the branch, merges it with `--no-ff` into the base
branch and pushes that. `--skip-checks` skips the checks, `--keep-branch` keeps
the branch.

## Machine setup

`wtx install-machine` prints what it would change. `--apply` writes it, with a
backup of each file. It sets up:

- `~/.bashrc`: `eval "$(wtx shell-init bash)"`.
- `~/.tmux.conf`: the status line, `prefix+s` and `prefix+g`.
- the global git ignore, so the generated files never show as untracked.
- `~/.config/wt/config.toml`: worktrees go under `.claude/worktrees/`.
- `~/.claude/agents/Explore.md`: the search agent on haiku.
- `~/.claude/settings.json`: the hooks that call `wtx notify` and
  `wtx handoff`.
- the skills `/wtx-init` and `/wtx-create` in `~/.claude/skills`.

The hooks and the Explore agent are machine-wide. They also apply to Claude
Code sessions that are not wtx worktrees.

`wtx doctor` checks the machine and the repo you are in, and gives a fix for
each failed check. `--json` for scripts.

`wtx shell-init bash` prints what `~/.bashrc` sources: tab completion for
`wtx`, and four short aliases: `wtgo` (`wtx go`), `wtdone` (`wtx done`),
`wtland` (`wtx land`) and `wtmon` (`wtx monitor`).

## Dry run and help

`wtx -n <command>` (or `--dry-run`) prints what would change and runs none of
it. The flag goes before the command: `wtx -n go feat/x`.

`wtx help` lists the commands, `wtx help <command>` shows one.

## Commands called by machines

You do not run these yourself. They are listed so nothing is a mystery.

| Command | Called by |
| --- | --- |
| `wtx hook post-create`, `post-checkout`, `pre-remove` | `.wt.toml`, when `wt` creates, checks out or removes a worktree |
| `wtx notify <state>` | the agent hooks, to record the session state |
| `wtx handoff` | the `ExitPlanMode` hook, to start the build |
| `wtx tmux-status` | the tmux status line |

## Every command

| Command | What |
| --- | --- |
| `wtx init` | write `wtx.toml` and `.wt.toml` for this repo |
| `wtx config [validate]` | check `wtx.toml` |
| `wtx go <branch> [base]` | branch, worktree, session, agent. Or `all`. |
| `wtx all` | bring every worktree's session back, detached |
| `wtx done [branch]` | close a worktree, keep the branch |
| `wtx land [branch]` | rebase, pull request, merge, clean up |
| `wtx open [family] [branch]` | print and open a dev server URL |
| `wtx curl [family] [path] [curl flags]` | curl one of this worktree's servers, without knowing its port |
| `wtx status [branch]` | ports, sessions and pairings |
| `wtx setup` | make a worktree runnable, again |
| `wtx teardown` | kill the session and free the ports |
| `wtx tmux` | create or attach this checkout's session |
| `wtx brief [branch] --prompt ...` | send a prompt to a worktree's agent pane |
| `wtx hook <event>` | called by `.wt.toml` |
| `wtx notify <state>` | called by the agent's hooks |
| `wtx handoff` | called by the agent's `ExitPlanMode` hook |
| `wtx tmux-status` | one line for tmux `status-right` |
| `wtx monitor` | every session on one screen |
| `wtx doctor` | check this machine |
| `wtx install-machine` | the shell, tmux and agent hook setup |
| `wtx shell-init [bash]` | print the shell functions to source |
| `wtx guard` | install or show the push guard |

`wtx init` has more options: `--scan` prints its guesses as JSON and writes
nothing,
`--edit` merges an existing `wtx.toml` with a fresh scan, `--from-json` writes
from answers (this is what `/wtx-init` uses), `--lab`, `--no-notes` and
`--force`. See `wtx help init`.
