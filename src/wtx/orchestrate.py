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
   which runs `wtx handoff`. That records which conversation is holding the
   accepted plan and how it was sized, then answers `continue: false` so the
   planning model stops there instead of starting to implement.
3. The Stop hook fires `wtx notify stop`, which drains the record: the agent
   pane is respawned on `claude -r <that conversation>`, on the implementation
   model, at the effort the plan's size asks for.

The implementation carries on the same conversation. Everything the planner
read to write the plan is still there, which is most of what implementing it
needs, and Claude Code's own opusplan switches models this way. The prompt
cache is lost across the switch either way, so the transcript is re-read once
and cached again.

The size routes effort, not the model. Anthropic's guidance is that effort is
usually the better lever and that model choice suits the kind of work rather
than the individual task, so `small_model` is empty until a repo has measured
that a smaller one is enough.

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
from .config import WtxConfig
from .proc import warn

SIZES = ("small", "large")
SIZE_MARKER = "wtx-size:"

# Marks the block wtx appends to a brief, so a reader can tell wtx's
# instructions to the planner from the human's own task.
INSTRUCTION_MARK = "<!-- wtx -->"

PLAN_INSTRUCTION = f"""
{INSTRUCTION_MARK}
Added by wtx. End your plan with this line, on its own, and nothing after it:

    {SIZE_MARKER} small

Say `small` when the plan is a handful of steps in code you have already read,
and `large` when it is not. wtx reads that line to decide how much effort the
implementation gets, so answer for the work, not for the writing.
"""

HANDOFF_PROMPT = (
    "The plan is accepted. Implement it, working through it in order. "
    "If a step turns out to be wrong, say so and stop rather than quietly "
    "doing something else."
)


def plan_instruction() -> str:
    return PLAN_INSTRUCTION


def size_of(plan: str) -> str:
    """What the planner called the job.

    The marker is read from the end of the plan, and tolerates the list bullets
    and bold markers a model wraps a line in. No marker means large: guessing
    from the length of the plan is a guess, and guessing low costs quality
    where guessing high only costs money.
    """
    for line in reversed(plan.strip().splitlines()):
        head, marker, rest = line.strip().lower().partition(SIZE_MARKER)
        if marker and not head.strip("*_-# "):
            word = rest.strip(" `*_.")
            if word in SIZES:
                return word
    return "large"


def model_for(cfg: WtxConfig, size: str) -> str:
    """The model that implements a plan of this size.

    Normally build_model whatever the size, with the size going to effort
    instead. A repo that has set small_model has opted into the other bet.
    """
    orch = cfg.agent.orchestration
    if size == "small" and orch.small_model:
        return orch.small_model
    return orch.build_model


# ---------------------------------------------------------------------------
# the record a handoff leaves behind


def record_file(session: str) -> Path:
    """Beside the state files, but not named *.json: the monitor reads those."""
    return notify.state_dir() / f"{notify.safe_name(session)}.handoff"


def write_record(session: str, payload: dict) -> None:
    with contextlib.suppress(OSError):
        record_file(session).write_text(json.dumps(payload))


def pending(session: str) -> dict:
    """A handoff waiting to fire, or one in flight. For `wtx status`."""
    for path in (record_file(session), _running_name(record_file(session))):
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _running_name(pending_path: Path) -> Path:
    return pending_path.with_name(pending_path.name + ".running")


def claim(session: str) -> Path | None:
    """Take the pending record, atomically, or return None.

    The Stop hook fires on every turn and more than one may see the same
    record. A rename means exactly one of them acts on it.
    """
    path = record_file(session)
    running = _running_name(path)
    try:
        path.rename(running)
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
    conversation = str(payload.get("session_id", ""))
    if not plan or not conversation:
        # With no conversation to resume there is no handoff to make, and
        # stopping the agent would only take the plan away.
        return ""
    if not (tmux.available() and tmux.has_session(ctx.session)):
        # Nothing to respawn: an agent in a plain terminal.
        return ""

    size = size_of(plan)
    model = model_for(ctx.cfg, size)
    effort = orch.small_effort if size == "small" else orch.large_effort
    if model == orch.plan_model and effort == ctx.cfg.agent.effort:
        return ""  # already the right model at the right effort, carry on

    write_record(
        ctx.session,
        {
            "session": ctx.session,
            "root": str(ctx.root),
            "conversation": conversation,
            "model": model,
            "size": size,
        },
    )
    return json.dumps(
        {
            "continue": False,
            "stopReason": f"wtx: {size} plan, continuing on {model} at {effort}",
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
    if not _spawn(running):
        warn("could not fork the handoff, running it in the hook's own pane")
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
    """Respawn the agent pane on the planning conversation, on the build model."""
    try:
        data = json.loads(record.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    record.unlink(missing_ok=True)
    try:
        ctx = context.load(root=Path(data["root"]))
        agent = agents.get(ctx.agent_tool)
    except (context.ContextError, config_mod.ConfigError, KeyError):
        return 0
    rctx = tmux.render_ctx(
        ctx,
        repos.resolve_all(ctx),
        llm=data["model"],
        phase="build",
        size=data["size"],
    )
    cmd = agent.handoff_cmd(rctx, session=data["conversation"], prompt=HANDOFF_PROMPT)
    if not cmd:
        warn(f"{agent.name} cannot resume a conversation, no handoff made")
        return 0
    tmux.respawn_agent(
        ctx,
        cmd,
        note=f"{ctx.session}: {data['size']} plan, continuing on "
        f"{rctx.model} at {rctx.effort or 'the default effort'}",
    )
    return 0


def main(argv: list[str]) -> int:  # pragma: no cover - process entry point
    if len(argv) == 2 and argv[0] == "--run":
        return run(Path(argv[1]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
