---
name: wtx-init
description: Set up wtx in this repository, with the human deciding. Use when the user asks to add wtx, set up worktrees and tmux sessions for agents, run /wtx-init, or update an existing wtx.toml (change the agent tool, reconfigure). Reads the repo, proposes a full config with reasons, asks about each choice, then writes the files through the wtx CLI.
---

# Setting up wtx in a repository

You read the repo and propose. The human decides. You never invent a value in
silence, and you never write a file the human has not seen a reason for.

`wtx` is the only thing that writes `wtx.toml`. You collect answers and hand
them over as JSON. That way there is one file format and one writer.

## 1. Check the machine

```sh
wtx doctor --json
```

If a hard check fails, show the fixes and stop. There is nothing useful to
configure on a machine where `wt` or `tmux` is missing.

## 2. Read the repo

```sh
wtx init --scan
```

That prints the guesses as JSON, including a `_notes` block with what it saw:
the package manager, whether uv is used, the make targets, the workflow
branches, the sibling repos it found, the k8s folder.

Then read, yourself, whatever the scan could not settle:

- `README.md` and `CLAUDE.md` for how the project is actually run
- the `Makefile`s and `package.json` scripts for the real dev and check commands
- `.github/workflows/*.yml` for which branches deploy and whether tags publish
- `docker-compose*.yml` for a database or other service a worktree would share
- `lefthook.yml` or `.pre-commit-config.yaml` for hooks that need installing

## 3. Ask

Use AskUserQuestion. One topic per question, the scan's guess first and marked
`(Recommended)`, with the reason in the description. Ask about these, skipping a
topic the scan settled beyond doubt:

1. **Base and protected branches.** Say why: "deploy.yml pushes on dev and main".
   Getting this wrong is the one that matters: the push guard, the deny rules and
   the completion all read it.
2. **Ports.** The families found and the values the main checkout keeps.
3. **Dependencies.** The install steps, and the Python pin if uv is used.
4. **Panes.** Which servers run in the session, and the command for each.
5. **Agent and models.** Tool (Claude Code or opencode, the trade-offs are in
   `references/questions.md`, and switching one for the other moves more keys
   than `tool` alone), the worker model, the subagent model, the context cap,
   and whether to plan on one model and build on another
   (`[agent.orchestration]`). Explain the trade: a big context window costs on
   every turn, a cheaper subagent model costs nothing in quality for
   searching, and planning on the strongest model is cheap because a plan is
   short.
6. **Extra deny rules.** Propose from the repo: `git tag` and `--tags` when a
   workflow publishes on `v*` tags, database migration commands when a shared
   database exists.
7. **External directories.** For each sibling repo and the k8s folder the scan
   found: `read`, `pair`, or skip. Explain the difference in one line, using
   `references/questions.md`. Ask for an `editable_install` when the sibling is
   a Python package this repo imports.
8. **Allow list.** The lint, test and build commands found. These skip the
   permission prompt, so only routine read-only or local commands belong here.
9. **Repo hooks.** Anything the scan cannot express, such as making a branch
   database or pulling Git LFS files. It becomes `[hooks].post_setup`.

Never ask about something you can read. Never ask twice about the same thing.

## 4. Write

Save the answers as JSON with the same shape as the scan output, then:

```sh
wtx init --from-json answers.json
wtx config validate
```

`wtx init` prints the app-side changes that are still needed: every hardcoded
port becomes a variable. Offer to make those edits, one file at a time, showing
the diff before each. They are ordinary code changes, so do them the ordinary
way.

It also prints a section for `CLAUDE.md` (or `AGENTS.md`). Add it.

## 5. Hand back

Tell the human to run the smoke test themselves, from a real terminal:

```sh
wtx go test/smoke --no-attach
cat .claude/worktrees/test-smoke/.env.worktree
cd .claude/worktrees/test-smoke
git commit --allow-empty -m "test: smoke" && git push      # works
git push --dry-run origin HEAD:<base branch>               # must be REFUSED
cd - && wtx done test/smoke
```

You cannot run it. Creating a worktree writes `.git/hooks` and talks to the tmux
socket, and a sandboxed agent can do neither. Say that plainly rather than
trying and reporting a confusing failure.

Then offer to commit, on a branch, never on the base branch.

## Re-running on a repo that has wtx

Same flow, a different start. Instead of a bare scan, run:

```sh
wtx init --edit
```

That merges the existing `wtx.toml` with a fresh scan and prints it as JSON,
with a `_changes` block next to `_notes` listing where the repo has moved on
since the file was written: the base branch, the protected branches, a family
port, a pane command, and a port family or pane the scan found that the file
has no entry for. The file's own values win, so nothing a human chose is
reset, and nothing is written yet.

- Ask about each `_changes` entry: the scan says the repo moved, the file says
  otherwise. This is where a drifted answer matters most: changing the base
  branch or the protected list moves the push guard with it.
- An entry with `"file": null` is a service the repo grew since. Its `scan`
  value is the whole block to add, so offer it as it stands. wtx never adds it
  on its own, it cannot tell a new service from one the human deleted.
- Then ask what brought the human here today (a tool switch, a new server, a
  model change) and follow the sections above for it.
- When the agent tool switches, carry the whole `[agent]` table over, not just
  `tool`. Which keys survive and which become dead is in
  `references/questions.md`.
- Write the same way, with `--force` this time, since the file already exists:

```sh
wtx init --from-json answers.json --force
wtx config validate
```

If the repo has no `wtx.toml`, `wtx init --edit` prints a plain scan and an
empty `_changes`. That is the first run flow; start at step 1.

## References

- `references/schema.md`, every key in `wtx.toml`
- `references/questions.md`, the wording for each question and the trade-offs
