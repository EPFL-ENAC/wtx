"""Running commands, with a global dry-run switch.

Every mutating command in wtx goes through run(). With --dry-run the command is
printed and not executed, so a smoke test can show what a real run would do
without pushing anything.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

DRY_RUN = False


def set_dry_run(value: bool) -> None:
    global DRY_RUN
    DRY_RUN = value


def is_dry_run() -> bool:
    """Always call this, never import DRY_RUN.

    `from .proc import DRY_RUN` copies the value at import time, so a later
    set_dry_run never reaches that module and a dry run removes things for
    real.
    """
    return DRY_RUN


def say(msg: str) -> None:
    print(f"wtx: {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"wtx: {msg}", file=sys.stderr)


class CommandError(Exception):
    def __init__(self, cmd: Sequence[str], code: int, output: str = ""):
        self.cmd = list(cmd)
        self.code = code
        self.output = output
        super().__init__(f"{shlex.join(self.cmd)} failed with {code}")


def _env(extra: Mapping[str, str] | None) -> dict[str, str] | None:
    if not extra:
        return None
    env = dict(os.environ)
    env.update(extra)
    return env


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    quiet: bool = False,
    mutating: bool = True,
) -> int:
    """Run a command, streaming its output. Returns the exit code."""
    if DRY_RUN and mutating:
        loc = f" (in {cwd})" if cwd else ""
        print(f"would run{loc}: {shlex.join(cmd)}")
        return 0
    stdout = subprocess.DEVNULL if quiet else None
    proc = subprocess.run(
        list(cmd), cwd=cwd, env=_env(env), stdout=stdout, stderr=None, check=False
    )
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode)
    return proc.returncode


def run_shell(
    command: str,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    mutating: bool = True,
) -> int:
    """Run a shell line, the way the old bash scripts did for config commands."""
    if DRY_RUN and mutating:
        loc = f" (in {cwd})" if cwd else ""
        print(f"would run{loc}: {command}")
        return 0
    proc = subprocess.run(
        ["sh", "-c", command], cwd=cwd, env=_env(env), check=False
    )
    if check and proc.returncode != 0:
        raise CommandError(["sh", "-c", command], proc.returncode)
    return proc.returncode


def capture(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = False,
) -> str:
    """Run a read-only command and return its stdout, stripped.

    Never suppressed by dry-run: reading is always safe and the rest of wtx
    needs the answer to print what it would do.
    """
    proc = subprocess.run(
        list(cmd),
        cwd=cwd,
        env=_env(env),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stderr.strip())
    return proc.stdout.strip()


def capture_code(
    cmd: Sequence[str], *, cwd: Path | None = None
) -> tuple[int, str, str]:
    proc = subprocess.run(
        list(cmd), cwd=cwd, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def which(name: str) -> str | None:
    from shutil import which as _which

    return _which(name)
