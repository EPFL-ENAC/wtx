# What breaks, and what wtx does about it

Every rule in this tool is here because something went wrong once. This is the
list, so nobody has to learn them twice.

## git and wt

**`$WT_MAIN` lies.** wt calls "main" whichever worktree holds the default
branch. Reading it puts scripts in the wrong repo. wtx never reads it and
resolves the main checkout with `git rev-parse --git-common-dir`. A test asserts
no module reads the variable.

**A new branch tracks its base.** `wt create x origin/dev` leaves `x` tracking
`origin/dev` with git's default `autoSetupMerge`. Then `git pull` rebases the
work onto dev and the next push is refused. wtx repairs this on every setup, not
only the first, because a branch can predate the fix.

**A local ref makes a stale base.** `wt create x somebranch` resolves whatever
that ref pointed at last time it was fetched. `wtx go` defaults to
`origin/<base>` and fetches first.

**A hook must be a list in `.wt.toml`.** `post_create = "wtx hook post-create"`
parses fine and then never runs, so a repo looks set up and has no ports, no
settings and no guard. It has to be `["wtx hook post-create"]`.

**`post_checkout` fires on create too**, in some wt builds. Every setup step
checks whether its work is already done.

**A half deleted worktree still shows in `git worktree list`.** Resolving a
branch to a path then gave the main checkout, where the guard exempts itself,
and a push to the base branch went through. `worktree_path_for` prunes first and
checks that the `.git` entry really exists.

## the push guard

**lefthook skips a pre-push job it thinks has no matching files.** It did that
on a real push to a protected branch, and a guard that can be skipped is not a
guard. wtx installs a plain `.git/hooks/pre-push`, always, and it is installed
after the dependency step so a hook manager is not the one that replaces it.

**A guard that reads a file from the repo is not a guard either.** A branch cut
before the tooling existed has no such file. The whole guard, protected branch
list included, is baked into the hook.

**`core.hooksPath` wins over `.git/hooks`.** A guard in the directory git
ignores never runs, so wtx follows the setting.

**An existing pre-push hook is kept** as `pre-push.before-wt` and still runs,
with the same refs on its stdin.

## the agent sandbox

**It cannot reach loopback.** Even with localhost in the allowed domains.
`sandbox.network.allowLocalBinding` is about binding a port on macOS, not about
reaching one, so there is nothing to switch on. `curl` and `docker` are excluded
from the sandbox and governed by the ask and allow rules instead.

**An ask rule beats an allow rule, so the agent could not POST to its own
backend.** `Bash(curl *-X*)` is in `ask` to catch a write to the internet, and
it catches `curl -X POST http://127.0.0.1:18065/items` with it. Rules are globs
over the command text with no way to say "not loopback", and a PreToolUse hook
cannot help either: a matching ask rule prompts whatever the hook answers. So
the agent gets a different verb. `wtx curl <family> [path] [args]` builds the
URL from the worktree's own ports, matches no ask rule, and is allowed with any
flags. It is narrower than the `curl http://localhost:*` rules it replaces,
which reach every other worktree's servers too.

**It cannot reach the tmux socket.** So server panes are mirrored to
`.wt-logs/*.log`, which is the agent's only window into why a server misbehaves.

**None of that is discoverable.** The ports are picked per worktree, the panes
are invisible, and the log files are named after panes. wtx writes them down in
`.claude/rules/wtx.md`, which Claude Code loads every session, and which is not
the repo's own `CLAUDE.md`: that one belongs to a human.

**It cannot write `.git/hooks`, nor the npm cache.** Creating a worktree is a
terminal job. wtx says so instead of leaving a repo quietly unguarded.

**It cannot write the machine-level settings**, which is right: those files
decide what it is allowed to do. `wtx install-machine` is for a human.

**An ask rule beats everything, including auto mode, and no allow overrides
it.** Never put a bare interpreter (`python3 -`, `node -`) in `ask`: an agent
writes files through heredocs and would prompt on every edit. A test asserts the
baseline has none.

**Project settings cannot carry hooks.** The notify hooks live in the user's own
settings file. They run in the agent's process, outside the Bash sandbox, which
is exactly why they can talk to tmux when the agent itself cannot.

## uv

**`uv run` re-syncs before running**, and `uv sync` is exact. Both put the locked
wheel back over an editable install. `UV_NO_SYNC=1` is added to `.env.worktree`
on its own whenever a repo does an editable install.

**uv does not look up the tree for `.python-version`.** Run from `backend/`, a
fresh sync grabs the newest interpreter on the machine, and a package with no
wheel for it tries to build from source. `[deps].python_version_file` is passed
explicitly.

## tmux

**A session name cannot contain `.` or `:`.** They become dashes.

**A pane's exported values leak into the next worktree.** Every pane exports
`.env.worktree`, so a `wtx go` run from inside a session hands `WTX_LLM` and
`WTX_AGENT` to the hook that sets up the new worktree, and the new one quietly
gets the old one's model. What `wtx go` passes to its hook travels under
`WTX_GO_*` names that no pane ever exports, and those are dropped before any
tmux server starts.

**Exported values leak into the tmux server.** If the command that starts the
server had a worktree's ports exported, every later pane in every session
inherits them, including the main checkout's. wtx scrubs them on every run.

**A blocked tmux socket still exits 0.** tmux prints the error on stderr and
returns success, so believing the exit code makes every session look present and
wtx creates none. Reachability is checked by looking at stderr too.

**A pane cannot be found by its title.** An agent rewrites its own pane title as
it works. Panes carry a `@wt_role` option instead.

**The `prefix+X` binding is server wide** and the last run owns it, whatever repo
it came from. It bakes in no path: the branch comes from the pane's own
directory at key time.

**Closing the session you are sitting in kills the command doing the closing.**
`wtx done` hands the job to the tmux server with `run-shell -b`.

## the plan handoff

**A hook cannot respawn its own pane.** `wtx handoff` runs inside the agent
process, in the agent pane. Restarting that pane kills whatever is running in
it, the hook included, halfway through. The handoff is recorded and a detached
process does the restart, the same trick the desktop notification uses.

**The Stop hook fires on every turn.** More than one of them can see the same
pending handoff, and briefing an agent twice throws away the first run. The
record is claimed by renaming it, so exactly one wins.

**A handoff record must not look like a state file.** `wtx tmux-status` and the
monitor read every `*.json` in the runtime directory and key them by session. A
record named `<session>.handoff.json` would sit in that list with no state. It
is `<session>.handoff`.

**A plan brief must not change the worktree's model.** The plan phase runs on
`plan_model`, but `.claude/settings.local.json` and a later `claude --continue`
keep the worktree's own model. Writing the planner into the settings file would
leave every later session on it.

**"PostToolUse cannot block" is about the tool, not the turn.** The hooks
reference lists `PostToolUse` as non-blocking, because the tool has already run
by then. A `{"continue": false, "stopReason": ...}` answer still ends the turn,
which is the whole handoff: checked against Claude Code 2.1.266 with a hook on
`Read`, where the hooked run stopped after the tool call and the same run
without the hook answered normally.

**The handoff resumes, it does not re-brief.** `claude -r <id>` continues the
conversation that wrote the plan, so the implementation still has everything the
planner read. A resumed session restores its own model and permission mode, so
`--model` and `--permission-mode` are passed explicitly to override them.

## landing

**Refusing when the base moved on makes the rebase dead code.** The old script
refused to land a branch unless `origin/dev` was already an ancestor of it,
which is false the moment dev gets a commit, so its own rebase only ever ran as
a no-op and everyone rebased by hand. `wtx land` rebases, aborts cleanly on a
conflict, and refuses only the case that check was for: a branch cut from a
newer protected branch (stage while dev lags) that would drag stage into dev.

## installing

**`uv tool install --force <path>` does not rebuild.** uv caches the built
wheel by version and reuses it, so the machine keeps running the first build
whatever the checkout says. For a local checkout use `--editable`, which
follows the working tree, or `--reinstall` to force a rebuild.

## everything else

**A generated file with no ignore line makes a worktree permanently dirty**, and
`wtx land` then refuses to land it. Every file wtx writes into a checkout is in
both ignore lists, the repo's `.gitignore` from `wtx init` and the machine-level
one from `wtx install-machine`. A test asserts a fresh setup leaves nothing
untracked.

**`.env.worktree` used to be rewritten whole**, so a key a human or a repo hook
added by hand was dropped on the next setup, including the automatic one. wtx
merges instead.

**`pkill -f uvicorn` kills every other worktree's servers.** Kill the process
tree you started, or the port.

**`from module import CONSTANT` copies the value.** A later change to the module
never reaches the importer. That is how a `--dry-run` once removed a worktree for
real. wtx calls `proc.is_dry_run()` and a test would catch it again.
