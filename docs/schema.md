# wtx.toml

One file per repo, tracked in git. Everything that differs between repos lives
here. Every key has a default, only `[repo]` really matters.

Check a file with `wtx config validate`.

## Top level

| Key | Default | What |
| --- | --- | --- |
| `schema_version` | `1` | Bumped when the shape changes. |
| `min_wtx_version` | none | Refuse to set up a worktree when the installed wtx is older. |

## `[repo]`

| Key | Default | What |
| --- | --- | --- |
| `name` | from the origin URL | Used in the session name and the port hash. Changing it moves every port. |
| `lab` | none | Fills `{lab}` in an external repo path. |
| `base_branch` | `main` | What a worktree is cut from, and what `wtx land` lands into. |
| `protected_branches` | `[base_branch]` | Branches a worktree may never push. The one source for the push guard, the agent's deny rules and the shell completion. |

## `[ports]` and `[[ports.family]]`

A worktree gets one offset, from a checksum of `<repo>/<branch>`, and one port
per family at that offset. The offset steps forward past ports another worktree
wrote and past anything listening.

| Key | Default | What |
| --- | --- | --- |
| `range_start` | `18000` | First family's low base. |
| `step` | `1000` | Gap between families. |
| `slots` | `500` | How many offsets exist. |

Each `[[ports.family]]`:

| Key | Default | What |
| --- | --- | --- |
| `name` | required | `backend`, `frontend`, anything. |
| `main` | required | The port the main checkout keeps. |
| `env` | `<NAME>_PORT` | The key written to `.env.worktree`. |

## `[seed]`

Gitignored files a fresh worktree needs, copied from the main checkout. Never
overwritten: a worktree may have changed its copy on purpose.

| Key | What |
| --- | --- |
| `copy` | Files to copy when missing. |
| `symlink` | Paths to link instead of copy, for large data. |
| `required` | Setup fails if these are missing from the main checkout. |

## `[deps]` and `[[deps.step]]`

| Key | Default | What |
| --- | --- | --- |
| `python_version_file` | none | Read and substituted for `{python_version}`. uv does not look up the tree for `.python-version`, so a uv step run from a subdirectory needs this. |
| `post_install` | none | Commands run after the steps. A failure is a warning, not an error. |

Each `[[deps.step]]`:

| Key | Default | What |
| --- | --- | --- |
| `run` | required | The command. `{python_version}` is substituted. |
| `if_missing` | none | Skip the step when this path already exists. |
| `cwd` | `.` | Relative to the worktree root. |

## `[env]`

| Key | What |
| --- | --- |
| `extra` | Static keys written into `.env.worktree`. `UV_NO_SYNC = "1"` is added on its own when a repo does an editable install. |
| `computed` | Keys a `[hooks]` script writes. wtx never sets them, it carries them over and unsets them in a checkout that has no `.env.worktree`. |

Anything else already in `.env.worktree` is kept as it is, comments included.

## `[panes]` and `[[panes.pane]]`

The pane with `role = "agent"` fills the left half. The rest stack on the right,
in the order they are listed.

| Key | Default | What |
| --- | --- | --- |
| `name` | required | Pane title. |
| `role` | `shell` | `agent`, `server` or `shell`. Exactly one `agent`. |
| `cwd` | `.` | Relative to the worktree root. |
| `cmd` | none | What the pane runs. Required for a `server`. |
| `log` | `false` | Mirror the pane to `.wt-logs/<name>.log`. The agent cannot reach the tmux socket, so this is how it sees a server. Leave it on for every server. |

Setup writes `.claude/rules/wtx.md` in the worktree, which Claude Code loads
every session: the ports this checkout got, the log file for each logged server
pane, and how to reach a server with `wtx curl`. None of it is discoverable from
the repository, and the file is gitignored.

## `[agent]`

| Key | Default | What |
| --- | --- | --- |
| `tool` | `claude` | `claude` or `opencode`. `wtx go --agent` overrides it for one worktree. |
| `llm` | none | The worker model. `wtx go --llm` overrides it. |
| `brief_permission_mode` | `plan` | A brief starts in plan mode, so the agent comes back with a plan instead of editing. |
| `subagent_model` | `sonnet` | Every subagent runs its own requests. |
| `effort` | none | `low` to `max`. |
| `auto_compact_window` | none | Compact at this many tokens. A long session re-sends its whole context every turn, so this is usually the biggest cost. |
| `disabled_mcp_servers` | none | Servers a worker never needs. |
| `explore_agent_model` | none | Writes a project Explore agent on that model. The built-in one inherits the main model. |

`brief_permission_mode` and `build_permission_mode` take the modes the agent
knows: `default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`.

`[agent.opencode]`: `provider` (prefix for `provider/model`), `small_model`,
`plan_agent`, `build_agent`.

## `[agent.orchestration]`

Plan on one model, implement on another, with no dance in between. Off unless
you turn it on.

A brief starts the agent on `plan_model`, in plan mode, and asks it to end the
plan with a `wtx-size:` line. When you accept the plan, wtx restarts the agent
pane on `claude -r <that conversation>`, on `build_model`, at the effort the
size asks for. Reading the plan stays your job; the model switching stops being
one.

| Key | Default | What |
| --- | --- | --- |
| `enabled` | `false` | Turns the whole thing on. |
| `plan_model` | `fable` | Writes the plan. The strongest model you have: a plan is cheap and a bad one is not. |
| `build_model` | `opus` | Implements it. |
| `small_effort` | `medium` | Effort for a plan the planner called `small`. |
| `large_effort` | `xhigh` | Effort for everything else. A plan with no `wtx-size:` line counts as large. |
| `small_model` | none | A different model for a small plan. Opt-in, see below. |
| `build_permission_mode` | `auto` | What the implementation starts in. The plan is already agreed, so the build does not stop to ask about each step. |

**The size routes effort, not the model.** Anthropic's guidance is that tuning
effort is usually a better lever than switching models, and that model choice
suits the kind of work you do rather than the task in front of you. So a small
plan gets the same model working less hard. `small_model` is there for a repo
that has measured that a smaller model is enough, and is empty until then.

**The implementation continues the planning conversation**, it does not start a
new one, which is how Claude Code's own `opusplan` switches models. Everything
the planner read while writing the plan is still there. The prompt cache is lost
across a model switch either way, so the transcript is re-read once and cached
again.

`[agent].llm` and `wtx go --llm` name the model for a plain session. A brief
takes its models from this block instead, and `wtx go` says so when both are
given.

It needs the `ExitPlanMode` hook from `wtx install-machine`. Without it nothing
ever fires and a brief simply runs on `plan_model`.

Subagents are a separate dial, and one that already exists: `subagent_model`
covers every subagent, `explore_agent_model` the search agent, which should be
the smallest model that can read.

## `[permissions]`

Added to the baseline wtx ships. A repo can tighten, never loosen: if it could
subtract, one typo would reopen force pushes.

| Key | What |
| --- | --- |
| `allow` | Routine commands that skip the prompt. |
| `ask` | Always prompts, even in auto mode. Never put a bare interpreter here: an agent writes files through heredocs and would prompt on every edit. |
| `deny` | Never. |
| `allowed_domains` | Added to the sandbox network allowlist. |

## `[checks]`

Run by `wtx land --local`. `lint` and `test`, each a list of shell commands.

## `[[repos]]`

An external directory the agent needs.

| Key | Default | What |
| --- | --- | --- |
| `name` | required | Used by `--with` and in the env keys. |
| `path` | required | Absolute, `~`-relative, or relative to the main checkout. `{lab}` and `{repo}` are expanded. |
| `access` | `read` | `read` or `pair`. |
| `base_branch` | `main` | What a paired branch is cut from. |
| `env_prefix` | `WTX_REPO_<NAME>` | Writes `<PREFIX>_PATH` and `<PREFIX>_BRANCH`. |
| `editable_install` | none | `{ cwd = "...", run = "..." }`, run after the dependency steps. `{path}` is the resolved directory. |

`read`: readable with no prompt, never writable.
`pair`: the same until someone runs `wtx go <branch> --with <name>=<branch>`,
which makes a worktree in that repo. `--with <name>` alone pairs it on a branch
named like the app branch. The agent edits there, and it lands through
that repo's own pull request. A pairing is remembered, so a later plain
`wtx go` never un-pairs a worktree, and `wtx done` never removes the paired one.

## `[hooks]`

This repo's own steps. They run through `sh`, from the worktree root, with the
worktree's environment.

| Key | When |
| --- | --- |
| `post_setup` | After `.env.worktree` is written, before the agent settings. Where a branch database or a Git LFS pull goes. |
| `pre_teardown` | Before the session is killed. |
