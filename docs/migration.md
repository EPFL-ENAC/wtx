# Migration

Two moves: from wt to workmux (every repo that uses wtx today), and off the
old bash scripts (a repo that never used wtx).

## From wt to workmux

wtx does not use wt any more. workmux makes the worktrees and the tmux
sessions, from a config wtx writes for it. What changes for you:

- The session of `feat/x` is `<repo>/feat-x`, not `<repo>/feat/x`. A live
  session keeps its old name, and the new wtx does not find it.
- A new branch with upper case, `_` or `.` gets a folder in lower case with
  dashes: `feat/Add_X` gives `.claude/worktrees/feat-add-x`. Old folders keep
  their name, and their session still gets the new name.
- `wtx monitor`, `prefix+s` (the wtx picker) and `prefix+g` (the grid) are
  gone. Use `wtx dashboard` or `workmux sidebar`. tmux's own `prefix+s`
  is back.
- `.wt.toml` is gone. wtx writes `.workmux.yaml` in the main checkout, ignored
  by git.

The worktrees, their branches, ports and `.env.worktree` do not move.

### 1. Finish what is running

A handoff record and a state file are named after the session. With the new
name, wtx never reads the old ones. So, before you start:

- Let every plan that is waiting go to its build. `wtx status` shows a handoff
  that is waiting.
- Note which sessions wait for you, the status line will forget them.

### 2. Close the old sessions

Kill each session, the worktrees stay: `prefix+X` then `k`, or
`tmux kill-session -t '=<repo>/feat/x'`. `tmux ls` lists them.

### 3. Update the machine

From a real terminal, not from an agent:

```sh
cargo install workmux --locked      # without --locked, cargo fails
uv tool install --reinstall git+https://github.com/EPFL-ENAC/wtx
wtx install-machine --apply
wtx doctor
```

`install-machine` comments out the old `prefix+s` and `prefix+g` lines in
`~/.tmux.conf`, adds `.workmux.yaml` to the global git ignore, and runs
`workmux setup --hooks` (the status hooks for the dashboard). If
`wtx doctor` warns about a workmux `merge` skill in `~/.claude/skills`, remove
it: it merges and removes the worktree around `wtx land`.

wt and `~/.config/wt/config.toml` can go.

### 4. Update each repo

In the main checkout of each repo that has a `wtx.toml`:

```sh
wtx init
```

On a repo set up already, it keeps `wtx.toml` as it is, removes `.wt.toml` and
adds `.workmux.yaml` to `.gitignore`. Commit the two changes the way that repo
takes changes to its base branch. A `.wt.toml` with more than the wtx hooks in
it stays, `wtx init` says so: remove the `wtx hook` lines by hand.

A paired repo with its own `wtx.toml` needs the same.

### 5. Bring the sessions back

```sh
wtx all
```

Every worktree gets its session back, under the new name. A Claude agent
starts on `claude --continue`, so each pane gets its last conversation back.
The state files of the old names are not read any more, and go at the next
logout (they live in `$XDG_RUNTIME_DIR`).

## Off the old bash scripts

For a repo that carries `scripts/wt-*.sh`, `scripts/tmux-dev.sh`,
`scripts/claude-worktree-settings.json` and `scripts/wt-go.bash`.

Do it on a branch, in the main checkout, and land it the usual way.

### 1. Close the worktrees, or leave them

Existing worktrees keep working: the hooks always run the main checkout's copy
of whatever tooling is there. But their `.env.worktree` was written by the old
scripts. wtx reads the ports from it and keeps them, so nothing moves.

### 2. Write the config

```sh
wtx init            # or /wtx-init in a Claude Code session
```

Compare the generated `wtx.toml` with the old `scripts/wt-lib.sh` constants:
`BASE_BRANCH`, `PROTECTED_BRANCHES`, `MAIN_BACKEND_PORT`, `MAIN_FRONTEND_PORT`.
Then compare the panes with `tmux-dev.sh`, and the allow list with
`scripts/claude-worktree-settings.json`.

### 3. Move what does not fit

Anything repo-specific that is not a config key becomes a hook:

```toml
[hooks]
post_setup = "scripts/wt-db.sh create"
pre_teardown = "scripts/wt-db.sh drop"
```

If that script writes keys into `.env.worktree`, list them so they are carried
over and unset properly:

```toml
[env]
computed = ["DB_NAME", "POSTGRES_TEST_DB"]
```

### 4. Delete the old files

```sh
git rm scripts/wt-lib.sh scripts/wt-setup.sh scripts/wt-teardown.sh \
       scripts/wt-new.sh scripts/wt-done.sh scripts/wt-land.sh \
       scripts/wt-open.sh scripts/tmux-dev.sh scripts/git-push-guard.sh \
       scripts/claude-worktree-settings.json scripts/wt-go.bash
```

Keep `scripts/wt-db.sh` or anything else a `[hooks]` line calls.

If `lefthook.yml` has a `pre-push` job running the old guard, delete that job.
The script it runs is gone, and wtx installs its own guard as a plain git hook
(lefthook once skipped a pre-push job for "no matching files"). The new guard
works next to lefthook, so the rest of `lefthook.yml` can stay.

Makefile targets become one-liners:

```make
go:       ; wtx go $(BRANCH)
wt-land:  ; wtx land $(BRANCH)
wt-done:  ; wtx done $(BRANCH)
```

Or drop them: `wtx go` and `wtx done` work from any repo.

### 5. Switch the shell

`wtx install-machine --apply` does it for you: it comments out the old
`source .../scripts/wt-go.bash` line in `~/.bashrc` and adds the new one. By
hand, it is:

```sh
# was: source ~/code/<some repo>/scripts/wt-go.bash
eval "$(wtx shell-init bash)"
```

One line for every repo, and the completion reads each repo's own
`protected_branches` instead of a hardcoded list.

### 6. Reinstall the guard and check

```sh
wtx guard
wtx doctor
```

Then, from a real terminal (not from an agent), make one worktree and close
it:

```sh
wtx go test/wtx --no-attach
wtx status
wtx done test/wtx
git branch -D test/wtx
```

## Keeping the tooling out of a repo

For a repo whose remote must not receive any of this, put the files in
`.git/info/exclude` instead of committing them:

```
wtx.toml
.workmux.yaml
.env.worktree
.wt-logs/
PROMPT.md
PROMPT.sent.md
PLAN.md
.claude/settings.local.json
.claude/agents/Explore.md
.claude/rules/wtx.md
.claude/worktrees/
opencode.json
```
