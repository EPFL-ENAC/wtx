# wtx

Every git branch gets its own worktree, ports, tmux session and coding agent
(Claude Code or opencode).

Why: you can run several agents at the same time, one per branch, and they do
not step on each other. Each one has its own checkout and its own dev server
ports. A push guard keeps the agent on its own branch, so it never pushes to
`main`. Landing the work is a human job, from the main checkout.

## Install

You need git, tmux, [workmux](https://github.com/raine/workmux), Python 3.11+,
Claude Code or opencode, and `gh` for `wtx land`. workmux makes the worktrees
and the tmux sessions. wtx gives it its config, and adds the ports, the agent
settings, the push guard, the plan then build handoff, review and land.

```sh
cargo install workmux --locked  # without --locked, cargo fails on a missing feature
uv tool install git+https://github.com/EPFL-ENAC/wtx
wtx install-machine --apply     # from a real terminal, not from an agent
wtx doctor
```

From a local checkout, install it editable so the tool follows the working
tree (`--force` alone reuses uv's cached build and changes nothing):

```sh
uv tool install --reinstall --editable ~/code/wtx
```

## Set up a repo

```sh
cd my-repo
wtx init                # or, in a Claude Code session: /wtx-init
wtx config validate
```

`wtx init` writes `wtx.toml` (tracked, your repo's choices live here) and the
`.gitignore` lines. It then prints the small changes to make in the app (every
port becomes an env var) and a block to paste into `CLAUDE.md`.

wtx writes the workmux config itself, from `wtx.toml`, to `.workmux.yaml` in
the main checkout (ignored by git). Do not edit it, `wtx go` writes it again.

## Every day

```sh
wtx go feat/thing                     # branch, worktree, session, agent, attached
wtx go feat/thing --prompt brief.md   # same, and the agent starts on that brief
wtx status                            # every worktree: ports, session, state
wtx dashboard                         # every session on one screen, Enter jumps
wtx review feat/thing                 # the diff in hunk, with the agent's notes
wtx land feat/thing                   # rebase, PR, merge, clean up (main checkout)
                                      # or prefix+X then l, from the branch session
                                      # or prefix+X then m: local merge, nothing pushed
wtx done feat/thing                   # close the worktree, keep the branch
```

wtx appends every plan that you accept to `PLAN.md` in the worktree. `wtx land`
posts the brief and the plans as one comment on the pull request before the
merge. `[repo].plans` sends them to the issue or to a file in `docs/`
instead, or turns this off.

## Docs

- [`docs/features.md`](docs/features.md), a short tour of every feature and command
- [`docs/schema.md`](docs/schema.md), every key in `wtx.toml`
- [`docs/traps.md`](docs/traps.md), why the rules are what they are (for wtx developers)
- [`docs/migration.md`](docs/migration.md), moving a repo from wt to workmux, or off the old bash scripts
