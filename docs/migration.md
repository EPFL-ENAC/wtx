# Moving a repo off the bash scripts

For a repo that carries `scripts/wt-*.sh`, `scripts/tmux-dev.sh`,
`scripts/claude-worktree-settings.json` and `scripts/wt-go.bash`.

Do it on a branch, in the main checkout, and land it the usual way.

## 1. Close the worktrees, or leave them

Existing worktrees keep working: the hooks always run the main checkout's copy
of whatever tooling is there. But their `.env.worktree` was written by the old
scripts. wtx reads the ports from it and keeps them, so nothing moves.

## 2. Write the config

```sh
wtx init            # or /wtx-init in a Claude Code session
```

Compare the generated `wtx.toml` with the old `scripts/wt-lib.sh` constants:
`BASE_BRANCH`, `PROTECTED_BRANCHES`, `MAIN_BACKEND_PORT`, `MAIN_FRONTEND_PORT`.
Then compare the panes with `tmux-dev.sh`, and the allow list with
`scripts/claude-worktree-settings.json`.

## 3. Move what does not fit

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

## 4. Delete the old files

```sh
git rm scripts/wt-lib.sh scripts/wt-setup.sh scripts/wt-teardown.sh \
       scripts/wt-new.sh scripts/wt-done.sh scripts/wt-land.sh \
       scripts/wt-open.sh scripts/tmux-dev.sh scripts/git-push-guard.sh \
       scripts/claude-worktree-settings.json scripts/wt-go.bash
```

Keep `scripts/wt-db.sh` or anything else a `[hooks]` line calls.

If `lefthook.yml` has a `pre-push` job running the old guard, delete that job.
wtx installs a plain git hook, and lefthook skipping a pre-push job is why.

Makefile targets become one-liners:

```make
go:       ; wtx go $(BRANCH)
wt-land:  ; wtx land $(BRANCH)
wt-done:  ; wtx done $(BRANCH)
```

Or drop them: `wtgo` and `wtdone` work from any repo.

## 5. Switch the shell

Replace the source line in `~/.bashrc`:

```sh
# was: source ~/code/<some repo>/scripts/wt-go.bash
eval "$(wtx shell-init bash)"
```

One line for every repo, and the completion reads each repo's own
`protected_branches` instead of a hardcoded list.

## 6. Reinstall the guard and check

```sh
wtx guard
wtx doctor
```

Then the smoke test from `README.md`, from a real terminal.

## Keeping the tooling out of a repo

For a repo whose remote must not receive any of this, put the files in
`.git/info/exclude` instead of committing them:

```
wtx.toml
.wt.toml
.env.worktree
.wt-logs/
PROMPT.md
PROMPT.sent.md
```
