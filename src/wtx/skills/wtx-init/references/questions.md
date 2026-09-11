# Wording for each question

Keep options short. Put the reason in the description, not in the label.

## Base and protected branches

The base branch is what a new worktree is cut from and what work lands into.
Protected branches are the ones a worktree may never push. This one list feeds
the push guard, the agent's deny rules and the shell completion, so it is worth
a moment.

Signals: a `deploy.yml` triggered on push to a branch, a branch protection rule,
a branch named dev, stage, main or prod.

## Ports

Every worktree gets its own port per family, from a hash of the repo and branch,
stepping past ports that are taken. The `main` value is what the main checkout
keeps, so a developer not using worktrees sees no change.

## Agent and models

- **Worker model.** What the agent uses on this repo. A strong model for real
  work, a cheaper one for a docs repo.
- **Subagent model.** Every subagent runs its own requests. A search agent does
  not need the top model.
- **Context cap** (`auto_compact_window`). The session compacts at this size.
  A long session re-sends its whole context on every turn, so this is usually
  the biggest cost in a day of work, more than the number of subagents.
- **Explore agent model.** The built-in search agent inherits the main model.
  A small model override makes searching much cheaper.
- **Plan then build** (`[agent.orchestration]`). Off by default. On, a brief
  plans on `plan_model` and, once the human accepts the plan, wtx carries the
  same conversation on to `build_model` to implement it, at the effort the
  plan's size asks for. It suits a repo where work usually starts from a brief,
  and costs nothing in a repo where it does not: the planning model is only
  strong for the plan. Note that the size routes effort, not the model, which
  is the lever Anthropic recommends reaching for first. It needs
  `wtx install-machine --apply` to have run.

## Servers the agent cannot see

The dev servers run in tmux panes, and a sandboxed agent reaches neither the
tmux socket nor a port from Bash. Two things follow, and both are automatic:

- `log = true` on every server pane, so the pane is mirrored to
  `.wt-logs/<name>.log`. That file is the agent's only view of a running server.
- `wtx curl <family> [path]` reaches a server without knowing its port, with any
  method and no prompt. Say so in the repo's CLAUDE.md: it is not guessable, and
  plain curl prompts on anything past a GET.

## read or pair

- **read**: the agent reads the directory with no prompt and can never write in
  it. Right for a shared config repo or a large data repo.
- **pair**: the same, plus, when someone runs `wtx go <branch> --with <name>`,
  its own worktree and branch in that repo. The agent edits there and the change
  goes through that repo's own pull request. Right for a sibling library this
  repo imports, and for the k8s config, where edits are reviewed.
- **skip**: the agent has no business in it.

Ask for an `editable_install` when a sibling is a Python package this repo
imports, so the paired branch is what the backend actually runs.

## Extra deny rules

The baseline already refuses force pushes, pushes to protected branches,
changing the remote, and every write through a connector. Ask about:

- `git tag` and `git push --tags` when a tag publishes a release
- migration or data-upload commands when a database is shared between checkouts

## Allow list

An allow rule skips the permission prompt. Only routine local commands belong
there: lint, format, type-check, test, build. Anything that reaches the network
or changes something outside the checkout does not.
