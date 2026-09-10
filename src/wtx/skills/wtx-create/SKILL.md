---
name: wtx-create
description: Start a worktree on a brief, with the right model and effort. Use when the user asks to spawn an agent on a task, open a worktree for something, run /wtx-create, kick off work in the background, or start several branches at once. Writes the brief, picks the plan effort, and runs wtx go.
---

# Starting a worktree on a brief

`wtx go <branch> --prompt "<brief>"` makes the worktree, the tmux session and
the agent, and starts that agent on the brief in plan mode. Your job is to turn
what the user said into a branch name, a brief, and one choice: how hard the
planning agent should think.

One worktree per call. For several tasks, run the command once per task.

## 1. Check the repo is set up

```sh
wtx config validate
```

If there is no `wtx.toml`, the repo is not set up. Say so and offer
`/wtx-init`. Do not write config yourself.

## 2. Name the branch

Read the existing branches first, and match how they are named:

```sh
git branch -a --sort=-committerdate | head -20
```

Usually `feat/<short-thing>`, `fix/<short-thing>`, `chore/<short-thing>`. Short,
kebab-case, no ticket number unless the repo uses them.

## 3. Write the brief

The brief is the whole context the agent gets. It reads the repo itself, so do
not paste code into it. Say:

- what to do, in the user's own terms
- where it lives, if you know (a file, a module, a command)
- what "done" looks like, and how to check it
- anything the user ruled out

Three to ten lines. If the user gave you a sentence, keep it a sentence. Do not
invent requirements they did not state, and do not turn a question into a task.

Long briefs go in a file, and `--prompt` takes the path:

```sh
wtx go feat/thing --prompt /path/to/brief.md --no-attach
```

## 4. Pick the plan effort

The agent plans first, on a fast model, then hands the accepted plan to the
implementation model by itself. `--plan-effort` is how hard it thinks while
planning. The default is `low`.

Keep `low` when the task is clear and lands in code the agent will find fast:
one file, a known command, a bug with a stack trace, a rename, a test to add.

Pass `--plan-effort high` when the plan itself is the hard part:

- it touches several parts of the repo, or code nobody has read recently
- there is a design choice to make, or more than one way to do it
- it changes an interface other things depend on
- the user says it is big, or asks for a refactor or a migration

`--plan-model` changes the planning model too, if the user asks for it.
Otherwise leave it: the repo config picks it.

## 5. Run it

```sh
wtx go <branch> --prompt "<brief>" --no-attach [--plan-effort high]
```

`--no-attach` always: attaching takes over the terminal and you cannot use it.

Creating a worktree writes `.git/hooks` and talks to the tmux socket. A
sandboxed agent can do neither, so the command may fail with a permission
error. If it does, say so plainly and give the user the exact line to run
themselves, rather than retrying it another way.

## 6. Hand back

Say what was started, and how to reach it:

```sh
wtx status                 # ports, sessions, and what each agent is doing
wtx monitor                # every session on one screen
tmux attach -t <session>   # the session name is printed by wtx go
```

The agent is waiting on its plan. Nothing is merged until a human runs
`wtx land <branch>` from the main checkout.
