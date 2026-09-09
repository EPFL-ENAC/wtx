"""What .wt.toml calls.

A consumer repo's .wt.toml is three lines:

    [hooks]
    post_create = ["wtx hook post-create"]
    post_checkout = ["wtx hook post-checkout"]
    pre_remove = ["wtx hook pre-remove"]

wt exports WT_PATH and WT_BRANCH and runs the hook through sh, with its own
working directory. WT_MAIN is also exported and never used: wt calls "main"
whichever worktree holds the default branch.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import context, git
from .proc import warn
from .repos import WITH_ENV, decode_with
from .setup import run_setup
from .teardown import run_teardown

EVENTS = ("post-create", "post-checkout", "pre-remove")

# What `wtx go` passes down to the hook. These are on purpose not the names
# written to .env.worktree: every pane exports that file, so a `wtx go` run
# from inside another worktree's session would otherwise hand that worktree's
# agent and model to the new one.
GO_AGENT_ENV = "WTX_GO_AGENT"
GO_LLM_ENV = "WTX_GO_LLM"
GO_DRIVING_ENV = "WTX_GO_DRIVING"


def _target(path_env: str = "WT_PATH") -> Path | None:
    raw = os.environ.get(path_env, "")
    return Path(raw) if raw else None


def run_hook(event: str) -> int:
    if event not in EVENTS:
        warn(f"unknown hook event {event!r}, expected one of {', '.join(EVENTS)}")
        return 2
    path = _target()
    if path is None or not path.exists():
        warn(f"WT_PATH is not set or missing ({path}), nothing to do")
        return 0

    branch = os.environ.get("WT_BRANCH", "") or git.current_branch(path)
    main = git.main_checkout(path)
    if main is None:
        warn(f"{path} is not inside a git repository")
        return 1
    try:
        ctx = context.load(root=path)
    except context.ContextError as exc:
        warn(str(exc))
        return 0  # a repo without wtx.toml is not ours to set up
    if branch and branch != ctx.branch:
        ctx.branch = branch

    if event == "pre-remove":
        run_teardown(ctx)
        return 0

    run_setup(
        ctx,
        with_repos=decode_with(os.environ.get(WITH_ENV, "")),
        agent_tool=os.environ.get(GO_AGENT_ENV, ""),
        llm=os.environ.get(GO_LLM_ENV, ""),
        # When wtx go is driving, it creates the session itself once the
        # brief is in place. Starting it here too meant an agent launched
        # twice on a briefed worktree.
        start_tmux=not os.environ.get(GO_DRIVING_ENV),
        attach=False,
        brief=False,
    )
    return 0
