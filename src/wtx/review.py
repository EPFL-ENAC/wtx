"""Review a branch in hunk, with notes from the agent that wrote it.

The agent that wrote a branch knows why each hunk is there. wtx asks it, in its
own conversation (a forked headless resume, never a new brief), for a hunk
sidecar: the plan in a few lines, the files in reading order, one line per
hunk. hunk shows those notes beside the diff. Notes the human types in hunk go
back to the same agent when hunk quits, or before with `wtx review --notes`.

The agent answers on stdout and wtx writes the file, beside the handoff
records. The agent sandbox cannot write the runtime dir, and a file in the
checkout would make the worktree dirty. See docs/traps.md.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import git, notify, repos, tmux
from .agents import base as agents
from .context import Ctx
from .proc import capture_code, is_dry_run, run, say, warn, which

# A review reads a whole branch. A big one takes the agent a few minutes.
TIMEOUT_SECONDS = 600
WINDOW = "review"
BUFFER = "wtx-review"
# hunk keeps the human's notes in memory only. While it runs they are copied
# this often, so quitting hunk does not lose them.
POLL_SECONDS = 1.0

REVIEW_PROMPT = """\
Write review notes for this branch, for a human who reads its diff in hunk.

Run exactly this command. Read more only when a hunk is not clear without it:
  git diff {rng}

Answer with one JSON object and nothing else. No prose, no code fence.
The shape:
{{"version": 1,
 "summary": "the plan you followed, in a few short lines",
 "files": [{{"path": "path/from/the/repo/root",
            "summary": "what changed in this file, one line",
            "annotations": [{{"newRange": [12, 20],
                             "summary": "what this hunk does, one line",
                             "rationale": "why, only when it is not plain",
                             "author": "{author}"}}]}}]}}

Rules:
- Put the files in the order a reviewer should read them, the most important first.
- One annotation per hunk. newRange is [c, c+d-1] from the hunk header @@ -a,b +c,d @@.
- A hunk that only removes lines (d is 0) gets oldRange [a, a+b-1] instead.
- Short summaries. Leave rationale out when the reason is plain.
- Do not change any file. This is a read only task.
"""

# The same shape, for an agent that can hold its answer to a schema.
SIDECAR_SCHEMA = {
    "type": "object",
    "required": ["version", "summary", "files"],
    "properties": {
        "version": {"type": "integer"},
        "summary": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "annotations"],
                "properties": {
                    "path": {"type": "string"},
                    "summary": {"type": "string"},
                    "annotations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["summary"],
                            "properties": {
                                "newRange": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "minItems": 2,
                                    "maxItems": 2,
                                },
                                "oldRange": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "minItems": 2,
                                    "maxItems": 2,
                                },
                                "summary": {"type": "string"},
                                "rationale": {"type": "string"},
                                "author": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
}


class ReviewError(Exception):
    """Something the user can act on. Printed without a traceback."""


# ---------------------------------------------------------------------------
# the files


def sidecar_file(session: str) -> Path:
    """The sidecar as hunk reads it. Not named *.json: the monitor and the
    status line read every *.json in this dir as a session state."""
    return notify.state_dir() / f"{notify.safe_name(session)}.review"


def meta_file(session: str) -> Path:
    """Which commit the sidecar describes, so a stale one can be told apart."""
    return notify.state_dir() / f"{notify.safe_name(session)}.review.meta"


def notes_file(session: str) -> Path:
    """The human's notes, as the review window last saw them in hunk."""
    return notify.state_dir() / f"{notify.safe_name(session)}.review.notes"


def sent_file(session: str) -> Path:
    """The notes already sent to the agent, so none goes twice."""
    return notify.state_dir() / f"{notify.safe_name(session)}.review.sent"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    """Through a temp file, so hunk or `wtx status` never read half a file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def load(session: str) -> dict:
    return _read_json(sidecar_file(session))


def save(session: str, data: dict, *, head: str, rng: str, agent: str) -> None:
    # The meta goes last: a sidecar with no meta reads as no review at all.
    _write(sidecar_file(session), data)
    _write(meta_file(session), {"head": head, "range": rng, "agent": agent, "time": time.time()})


def status(session: str, head: str, rng: str = "") -> str:
    """none, stale (the branch moved on, or another range) or ok."""
    meta = _read_json(meta_file(session))
    if not meta or not sidecar_file(session).is_file():
        return "none"
    if meta.get("head") != head or (rng and meta.get("range") != rng):
        return "stale"
    return "ok"


def _unlink(*files: Path) -> None:
    for f in files:
        with contextlib.suppress(OSError):
            f.unlink(missing_ok=True)


def clear(session: str) -> None:
    """Teardown calls it. A review left behind would show on the next session
    with the same name."""
    _unlink(sidecar_file(session), meta_file(session), notes_file(session), sent_file(session))


def _notes_in(path: Path) -> list[dict]:
    notes = _read_json(path).get("notes")
    return [n for n in notes if isinstance(n, dict)] if isinstance(notes, list) else []


# ---------------------------------------------------------------------------
# asking the agent


def head(path: Path) -> str:
    return git.git(["rev-parse", "HEAD"], cwd=path)


def diff_range(ctx: Ctx, base: str = "") -> str:
    """What `wtx land` will merge: the branch since it left origin/<base>.

    Three dots, so commits that landed on the base since then do not show as
    removed lines.
    """
    if base:
        return f"{base}...HEAD"
    git.fetch(ctx.main)
    name = ctx.cfg.repo.base_branch
    remote = f"origin/{name}"
    code, _, _ = capture_code(["git", "rev-parse", "--verify", "--quiet", remote], cwd=ctx.root)
    return f"{remote if code == 0 else name}...HEAD"


def is_empty(path: Path, rng: str) -> bool:
    code, _, _ = capture_code(["git", "diff", "--quiet", rng], cwd=path)
    return code == 0


def extract_sidecar(text: str) -> dict:
    """The JSON object in an answer. Agents add a fence or a sentence around
    it whatever the prompt says, so take the first { to the last }."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ReviewError(f"the agent's answer has no JSON in it: {_short(text)}")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReviewError(f"the agent's answer is not valid JSON ({exc}): {_short(text)}") from exc
    if not isinstance(data, dict):
        raise ReviewError("the agent's answer is not a JSON object")
    return data


def _range_problem(value: object) -> str:
    """hunk's own rules for a range. It refuses the whole file on one bad range."""
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(n, int) and not isinstance(n, bool) for n in value)
    ):
        return "is not two whole numbers"
    if value[0] < 1 or value[1] < value[0]:
        return f"{value} is not a range of lines from 1 up"
    return ""


def validate(data: dict) -> list[str]:
    """What hunk would refuse, said before it opens on a blank screen."""
    problems: list[str] = []
    if data.get("version", 1) != 1:
        problems.append(f"version is {data.get('version')!r}, hunk reads version 1")
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        problems.append("no summary")
    files = data.get("files")
    if not isinstance(files, list) or not files:
        problems.append("no files")
        return problems
    for i, f in enumerate(files):
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not f["path"]:
            problems.append(f"file {i + 1} has no path")
            continue
        notes = f.get("annotations", [])
        if not isinstance(notes, list):
            problems.append(f"{f['path']}: annotations is not a list")
            continue
        for j, note in enumerate(notes):
            where = f"{f['path']} note {j + 1}"
            if not isinstance(note, dict):
                problems.append(f"{where} is not an object")
                continue
            if not isinstance(note.get("summary"), str) or not note["summary"].strip():
                problems.append(f"{where} has no summary")
            if "newRange" not in note and "oldRange" not in note:
                problems.append(f"{where} has no newRange")
            for key in ("newRange", "oldRange"):
                if key in note and (bad := _range_problem(note[key])):
                    problems.append(f"{where}: {key} {bad}")
    return problems


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _shown(argv: list[str]) -> str:
    """The command for a dry run, without the prompt and the schema in full."""
    return shlex.join(a if len(a) < 80 and "\n" not in a else f"<{len(a)} chars>" for a in argv)


def fresh(ctx: Ctx, base: str = "", *, force: bool = False) -> tuple[dict, str]:
    """The review of the branch as it is now, and its range. The agent is
    asked only when there is none, or with `force`."""
    rng = diff_range(ctx, base)
    if is_empty(ctx.root, rng):
        hint = "" if git.is_clean(ctx.root) else ", commit the work first"
        raise ReviewError(f"nothing to review, {rng} has no changes{hint}")
    if not git.is_clean(ctx.root):
        warn(f"{ctx.root} has uncommitted changes, the review only covers commits")
    if not force and status(ctx.session, head(ctx.root), rng) == "ok":
        say("the notes are up to date with the branch, --force asks again")
        return load(ctx.session), rng
    agent = agents.get(ctx.agent_tool)
    rctx = tmux.render_ctx(ctx, repos.resolve_all(ctx))
    return generate(ctx, agent, rctx, rng), rng


def generate(ctx: Ctx, agent, rctx, rng: str) -> dict:
    """Ask the branch agent for its notes, in its own conversation.

    Empty in a dry run, which only prints the command.
    """
    prompt = REVIEW_PROMPT.format(rng=rng, author=agent.name)
    argv = agent.review_cmd(rctx, prompt=prompt)
    if not argv:
        raise ReviewError(f"{agent.name} cannot write a review yet")
    if is_dry_run():
        print(f"would run (in {ctx.root}): {_shown(argv)}")
        return {}
    say(f"asking the agent of {ctx.session} for its notes, this can take a few minutes")
    # Its hooks see this and do nothing. A Stop from here would tell the human
    # the session waits on them, and fire a handoff that is not due.
    env = {**os.environ, notify.HEADLESS_ENV: "1"}
    try:
        proc = subprocess.run(
            argv,
            cwd=ctx.root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ReviewError(f"{argv[0]} is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReviewError(f"the agent did not answer in {TIMEOUT_SECONDS // 60} minutes") from exc
    if proc.returncode != 0:
        said = proc.stderr.strip() or proc.stdout.strip()
        raise ReviewError(f"{argv[0]} failed with {proc.returncode}: {_short(said)}")
    data = extract_sidecar(agent.review_result(proc.stdout))
    problems = validate(data)
    if problems:
        joined = "\n  - ".join(problems)
        raise ReviewError(f"hunk cannot use the agent's notes:\n  - {joined}")
    save(ctx.session, data, head=head(ctx.root), rng=rng, agent=agent.name)
    return data


# ---------------------------------------------------------------------------
# showing it


def hunk_binary() -> str | None:
    """hunk on PATH, or where its installer puts it. prefix+r runs wtx with
    the tmux server's PATH, which is the one from when the server started and
    may be older than the install."""
    found = which("hunk")
    if found:
        return found
    home = Path.home() / ".hunk" / "bin" / "hunk"
    return str(home) if os.access(home, os.X_OK) else None


def _hunk_argv(binary: str, session: str, rng: str) -> list[str]:
    return [binary, "diff", rng, "--agent-context", str(sidecar_file(session))]


def open_in_hunk(ctx: Ctx, rng: str) -> bool:
    """hunk in a `review` window of the branch session, or in this terminal
    when the session is down. False when hunk is not installed.

    A window and not a split: a diff wants the whole screen, and the agent
    pane keeps its size.
    """
    binary = hunk_binary()
    if binary is None:
        return False
    argv = _hunk_argv(binary, ctx.session, rng)
    if is_dry_run() or not (tmux.available() and tmux.has_session(ctx.session)):
        if is_dry_run():
            run(argv, cwd=ctx.root, check=False)
        else:
            watch_hunk(ctx, argv)
        return True
    open_window(ctx)
    tmux.attach(ctx.session)
    return True


def open_window(ctx: Ctx) -> None:
    """The `review` window, in place of the last one. It runs
    `wtx review --hunk-window`, see run_window.

    The same wtx that runs now, and its PATH: the window gets the tmux
    server's environment, which may not have the dir hunk was installed in.
    """
    window = [sys.executable, "-m", "wtx", "review", ctx.branch, "--hunk-window"]
    tmux.cmd(["kill-window", "-t", f"={ctx.session}:{WINDOW}"])
    tmux.cmd(
        [
            "new-window",
            "-t",
            f"={ctx.session}:",
            "-n",
            WINDOW,
            "-c",
            str(ctx.root),
            "-e",
            f"PATH={os.environ.get('PATH', '')}",
            shlex.join(window),
        ]
    )


def run_window(ctx: Ctx) -> int:
    """What the review window runs: hunk on the saved review, then the notes
    back to the agent.

    prefix+r opens the window before any review exists, so the agent is
    asked here when the saved one does not match the branch. Same rule as
    `wtx review`: the saved one is used as long as the branch did not move.
    """
    try:
        binary = hunk_binary()
        if binary is None:
            raise ReviewError("hunk is not installed (https://hunk.dev)")
        rng = _read_json(meta_file(ctx.session)).get("range", "")
        if not rng or status(ctx.session, head(ctx.root), rng) != "ok":
            _, rng = fresh(ctx)
    except ReviewError as exc:
        say(str(exc))
        _hold()
        return 1
    return watch_hunk(ctx, _hunk_argv(binary, ctx.session, rng), hold=True)


def _hold() -> None:
    """Wait for Enter in the review window, or it closes before anyone reads
    what went wrong."""
    if sys.stdin.isatty():
        with contextlib.suppress(EOFError, KeyboardInterrupt, OSError):
            input("press Enter to close this window ")


class _Closed(Exception):
    """The window is going away under us."""


def _closed(*_: object) -> None:
    raise _Closed


def watch_hunk(ctx: Ctx, argv: list[str], *, hold: bool = False) -> int:
    """Run hunk here, copy the human's notes while it runs, and send them to
    the agent pane when it quits.

    hunk keeps the notes in memory only, and quitting is how a review ends,
    so they are copied every POLL_SECONDS. A note saved less than that before
    quitting can be missed. The copy only grows: hunk drops its notes while it
    shuts down, and a copy taken then is empty. So a note deleted in hunk
    after it was copied still goes. Closing the window (Ctrl-b &, or a new wtx review
    that replaces it) is a SIGHUP, and ends the review the same way. `hold`
    waits for Enter on a failure, or the window would close before anyone
    reads why.
    """
    session = ctx.session
    # A new hunk starts with no notes: what the last one saw or sent is old.
    _unlink(notes_file(session), sent_file(session))
    proc = subprocess.Popen(argv, cwd=ctx.root)
    # Ctrl-C reaches hunk and this process alike. hunk decides what it
    # means, this one must live on to send the notes.
    handlers = {
        signal.SIGINT: lambda *_: None,
        signal.SIGHUP: _closed,
        signal.SIGTERM: _closed,
    }
    before = {sig: signal.signal(sig, handler) for sig, handler in handlers.items()}
    closed = False
    seen: list[dict] = []
    try:
        while True:
            try:
                proc.wait(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                pass
            # Fails until hunk has joined its daemon. The last copy stays.
            with contextlib.suppress(ReviewError):
                new = [n for n in collect_notes(ctx.root) if n not in seen]
                if new:
                    seen += new
                    _write(notes_file(session), {"notes": seen})
    except _Closed:
        closed = True
        with contextlib.suppress(OSError):
            proc.terminate()
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)
    try:
        sent = deliver(ctx, _notes_in(notes_file(session)))
    except ReviewError as exc:
        if closed:
            return 1  # no terminal left to say it on, the notes stay for --notes
        say(f"{exc}. The notes are kept, wtx review {ctx.branch} --notes sends them")
        if hold:
            _hold()
        return 1
    if sent and not closed:
        say(f"sent {plural(len(sent), 'note')} to the agent of {session}")
    return 0 if closed else proc.returncode


def print_fallback(data: dict, path: Path, rng: str) -> None:
    """Without hunk: the notes as text, then the plain diff."""
    if data.get("summary"):
        print(data["summary"].rstrip(), end="\n\n")
    for f in data.get("files", []):
        print(f"{f['path']}: {f.get('summary', '')}".rstrip(": "))
        for note in f.get("annotations", []):
            lines = note.get("newRange") or note.get("oldRange") or ["?"]
            print(f"  line {lines[0]}: {note.get('summary', '')}")
    if data:
        print()
    # git writes straight to the terminal. Piped, our lines would come after it.
    sys.stdout.flush()
    run(["git", "--no-pager", "diff", rng], cwd=path, check=False, mutating=False)


# ---------------------------------------------------------------------------
# notes back to the agent


def collect_notes(path: Path) -> list[dict]:
    """The notes the human typed in the live hunk session of this checkout.

    `--type user` and not `--type all`: all also returns the agent's own notes,
    and sending those back asks the agent to fix its own explanations.
    """
    binary = hunk_binary()
    if binary is None:
        raise ReviewError("hunk is not installed (https://hunk.dev)")
    code, out, err = capture_code(
        [binary, "session", "comment", "list", "--repo", str(path), "--type", "user", "--json"]
    )
    if code != 0:
        raise ReviewError(f"hunk is not open on {path}: {_short(err or out)}")
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise ReviewError(f"hunk said something that is not JSON: {_short(out)}") from exc
    items = data.get("comments", []) if isinstance(data, dict) else data
    notes = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("body") or item.get("summary") or item.get("title") or "").strip()
        if not text:
            continue
        lines = item.get("newRange") or item.get("oldRange")
        line = lines[0] if isinstance(lines, list) and lines else item.get("line")
        path_of = str(item.get("filePath") or item.get("file") or "")
        notes.append({"file": path_of, "line": line, "text": text})
    return notes


def current_notes(ctx: Ctx) -> list[dict]:
    """The notes in the open hunk. When it is closed, the last ones the review
    window copied: they are still there if sending them failed."""
    try:
        return collect_notes(ctx.root)
    except ReviewError:
        saved = _notes_in(notes_file(ctx.session))
        if saved:
            return saved
        raise


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def format_notes(notes: list[dict]) -> str:
    count = plural(len(notes), "item")
    out = [f"Review notes from hunk, {count}. Fix each one in this conversation:"]
    for note in notes:
        where = note["file"] + (f":{note['line']}" if note.get("line") else "")
        body = note["text"].replace("\n", "\n  ")
        out.append(f"- {where} {body}" if where else f"- {body}")
    return "\n".join(out)


def deliver(ctx: Ctx, notes: list[dict]) -> list[dict]:
    """Send the notes not sent yet, and return them. `--notes` while hunk is
    open, then quitting hunk, must not send the same note twice."""
    sent = _notes_in(sent_file(ctx.session))
    new = [n for n in notes if n not in sent]
    if not new:
        return []
    if not send_notes(ctx, format_notes(new)):
        raise ReviewError(f"no agent pane in {ctx.session}, start it with wtx go {ctx.branch}")
    _write(sent_file(ctx.session), {"notes": sent + new})
    return new


def send_notes(ctx: Ctx, text: str) -> bool:
    """Paste the notes in the agent pane and submit them. False with no pane.

    A bracketed paste (-p): typed with send-keys, the first newline would
    submit the message halfway.
    """
    if not tmux.available() or not tmux.has_session(ctx.session):
        return False
    pane = tmux.agent_pane_id(ctx.session)
    if not pane:
        return False
    fd, tmp = tempfile.mkstemp(prefix="wtx-review-", suffix=".txt")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        tmux.cmd(["load-buffer", "-b", BUFFER, tmp])
        tmux.cmd(["paste-buffer", "-p", "-d", "-b", BUFFER, "-t", pane])
        tmux.cmd(["send-keys", "-t", pane, "C-m"])
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    return True
