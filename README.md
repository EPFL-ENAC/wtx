# wtx

One branch, one git worktree, one tmux session, one coding agent.

`wtx` gives every branch its own checkout, its own ports, its own agent settings
and its own tmux session. The agent works on that branch and nothing else: a
pre-push hook stops it from pushing anywhere but its own branch, and landing the
work is a human job from the main checkout.

It works with Claude Code and with opencode, and a repo picks which one, and
which model, in one config file.

## Install

```sh
uv tool install git+https://github.com/EPFL-ENAC/wtx
wtx install-machine --apply     # from a real terminal, not from an agent
wtx doctor
```

## Add it to a repo

```sh
cd my-repo
wtx init                # or, in a Claude Code session: /wtx-init
```

That writes `wtx.toml` (tracked, this is where your repo's choices live) and a
three line `.wt.toml`, and prints the small app-side changes to make: every port
becomes a variable.

## Every day

```sh
wtgo feat/thing                     # branch, worktree, session, agent, attached
wtgo feat/thing --prompt brief.md   # same, and the agent starts on that brief
wtgo feat/thing --with k8s=feat/thing   # plus a paired worktree in the k8s repo
wtgo all                            # bring every session back after a reboot
wtx monitor                         # every session on one screen
wtx land feat/thing                 # rebase, PR, squash merge, clean up
wtdone feat/thing                   # close the worktree, keep the branch
```

## Why

Read `docs/traps.md`. Every rule in this tool is there because something went
wrong once.

## Documentation

- `docs/schema.md`, every key in `wtx.toml`
- `docs/traps.md`, what breaks and what wtx does about it
- `docs/migration.md`, moving a repo off the old bash scripts
