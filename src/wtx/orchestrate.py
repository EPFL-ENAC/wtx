"""Plan on one model, implement on another.

The dance this replaces: pick the planning model, write the brief, read the
plan, accept it, stop the agent, switch to the implementation model, type "go
ahead". Only reading the plan is a human job. The rest is bookkeeping, and
bookkeeping is what wtx is for.

How it runs:

1. `wtx go <branch> --prompt ...` writes PROMPT.md and starts the agent pane on
   `[agent.orchestration].plan_model`, in plan mode. The brief carries one extra
   instruction: end the plan with a `wtx-size:` line.
2. The human accepts the plan. Claude Code fires PostToolUse on ExitPlanMode,
   which runs `wtx handoff`. That records the approved plan and the model the
   work asks for, and answers `continue: false` so the planning model stops
   there instead of starting to implement.
3. The Stop hook fires `wtx notify stop`, which drains the record: the agent
   pane is respawned on the implementation model with the plan as its brief.

The build phase is a new conversation, not a resumed one. A planning
conversation is mostly the reading that produced the plan, it is re-sent whole
on every later turn, and the plan is the part that matters. So the plan is the
handover, and it is written into PROMPT.md like any other brief.

Nothing here happens unless a repo sets `[agent.orchestration].enabled`.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path

from . import config as config_mod
from . import context, notify, repos, tmux
from .agents import base as agents
from .agents.base import PROMPT_FILE, PROMPT_SENT
from .config import WtxConfig

SIZES = ("small", "large")
SIZE_MARKER = "wtx-size:"

# Marks the block wtx appends to a brief, so the build brief can quote the
# human's task without wtx's own instructions to the planner.
INSTRUCTION_MARK = "<!-- wtx -->"

PLAN_INSTRUCTION = f"""
{INSTRUCTION_MARK}
Added by wtx. End your plan with this line, on its own, and nothing after it:

    {SIZE_MARKER} small

Say `small` when the plan is a handful of steps in code you have already read,
and `large` when it is not. wtx reads that line to pick the model that
implements the plan, so answer for the work, not for the writing.
"""

BUILD_BRIEF = """The plan below was written in plan mode and accepted. Implement it.

Work through it in order. If a step turns out to be wrong, say so and stop
rather than quietly doing something else.

## The task

{task}

## The accepted plan

{plan}
"""


def plan_instruction() -> str:
    return PLAN_INSTRUCTION


def strip_instruction(text: str) -> str:
    """The human's half of a brief wtx added to."""
    return text.split(INSTRUCTION_MARK, 1)[0].strip()


def size_of(plan: str, *, small_words: int) -> str:
    """What the planner called the job, or, failing that, how long the plan is.

    The marker is read from the end of the plan, and tolerates the list bullets
    and bold markers a model wraps a line in.
    """
    for line in reversed(plan.strip().splitlines()):
        head, marker, rest = line.strip().lower().partition(SIZE_MARKER)
        if marker and not head.strip("*_-# "):
            word = rest.strip(" `*_.")
            if word in SIZES:
                return word
    return "small" if len(plan.split()) < small_words else "large"


def model_for(cfg: WtxConfig, size: str) -> str:
    orch = cfg.agent.orchestration
    return orch.small_model if size == "small" else orch.build_model


def build_brief(*, task: str, plan: str) -> str:
    return BUILD_BRIEF.format(task=task or "See the plan.", plan=plan.strip())


# ---------------------------------------------------------------------------
# the record a handoff leaves behind


def record_file(session: str) -> Path:
    """Beside the state files, but not named *.json: the monitor reads those."""
    return notify.state_dir() / f"{notify.safe_name(session)}.handoff"


def write_record(session: str, payload: dict) -> None:
    with contextlib.suppress(OSError):
        record_file(session).write_text(json.dumps(payload))


def claim(session: str) -> Path | None:
    """Take the pending record, atomically, or return None.

    The Stop hook fires on every turn and more than one may see the same
    record. A rename means exactly one of them acts on it.
    """
    pending = record_file(session)
    running = pending.with_name(pending.name + ".running")
    try:
        pending.rename(running)
    except OSError:
        return None
    return running


# ---------------------------------------------------------------------------
# the hook: capture an accepted plan


def capture(payload: dict, *, cwd: Path | None = None) -> str:
    """PostToolUse on ExitPlanMode. Returns what the hook should print.

    An empty string means wtx has nothing to say and the agent carries on, the
    answer for every repo that has not turned orchestration on.
    """
    where = Path(payload.get("cwd") or cwd or Path.cwd())
    try:
        ctx = context.load(cwd=where)
    except (context.ContextError, config_mod.ConfigError):
        return ""  # not a wtx checkout, not ours to orchestrate
    orch = ctx.cfg.agent.orchestration
    if not orch.enabled:
        return ""
    plan = str(payload.get("tool_input", {}).get("plan", "")).strip()
    if not plan:
        return ""
    if not (tmux.available() and tmux.has_session(ctx.session)):
        # Nothing to respawn: an agent in a plain terminal. Stopping it here
        # would leave the human with a plan and no way to start on it.
        return ""

    size = size_of(plan, small_words=orch.small_words)
    model = model_for(ctx.cfg, size)
    if model == orch.plan_model:
        return ""  # the planner is already the right model for the job

    sent = ctx.root / PROMPT_SENT
    task = strip_instruction(sent.read_text()) if sent.is_file() else ""
    write_record(
        ctx.session,
        {
            "session": ctx.session,
            "root": str(ctx.root),
            "model": model,
            "size": size,
            "brief": build_brief(task=task, plan=plan),
        },
    )
    return json.dumps(
        {
            "continue": False,
            "stopReason": f"wtx: {size} plan, handing it to {model}",
        }
    )


# ---------------------------------------------------------------------------
# the handoff itself


def drain(session: str) -> bool:
    """Fire a handoff recorded for this session, if there is one.

    The work happens in a detached process. Respawning the agent pane kills
    everything running in it, and this is called from a hook that runs there.
    """
    running = claim(session)
    if running is None:
        return False
    started = _spawn(running)
    if not started:  # no way to fork: do it here and hope the pane waits
        run(running)
    return True


def _spawn(record: Path) -> bool:
    cmd = [sys.executable, "-m", "wtx.orchestrate", "--run", str(record)]
    try:
        subprocess.Popen(  # noqa: S603
            cmd,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return True


def run(record: Path) -> int:
    """Respawn the agent pane on the implementation model, briefed with the plan."""
    try:
        data = json.loads(record.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    record.unlink(missing_ok=True)
    root = Path(data["root"])
    try:
        ctx = context.load(root=root)
        agent = agents.get(ctx.agent_tool)
    except (context.ContextError, config_mod.ConfigError, KeyError):
        return 0
    try:
        (root / PROMPT_FILE).write_text(data["brief"])
    except OSError:
        return 0
    resolved = repos.resolve_all(ctx)
    rctx = tmux.render_ctx(ctx, resolved, llm=data["model"], phase="build")
    tmux.send_brief(ctx, agent, rctx)
    return 0


def main(argv: list[str]) -> int:  # pragma: no cover - process entry point
    if len(argv) == 2 and argv[0] == "--run":
        return run(Path(argv[1]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
