"""Deterministic per-worktree ports.

Each worktree gets one offset, derived from a checksum of "<repo>/<branch>", and
one port per family at that offset. The same branch name in two repos does not
collide because the repo name is in the hash. The offset then steps forward past
ports another worktree already wrote in its .env.worktree, and past anything
listening on the machine.
"""

from __future__ import annotations

from pathlib import Path

from . import envfile, git
from .config import WtxConfig
from .proc import capture


def cksum(data: bytes) -> int:
    """POSIX cksum, the same number the old bash scripts hashed with.

    Keeping the algorithm means a worktree created by wtx lands on the port the
    bash version would have picked.
    """
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    length = len(data)
    while length:
        crc ^= (length & 0xFF) << 24
        length >>= 8
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    return (~crc) & 0xFFFFFFFF


def listening_ports() -> set[int]:
    """TCP ports something is listening on. Empty when ss is unavailable."""
    out = capture(["ss", "-Hltn"])
    found: set[int] = set()
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        local = fields[3]
        _, _, port = local.rpartition(":")
        if port.isdigit():
            found.add(int(port))
    return found


def ports_in_use_by_others(cfg: WtxConfig, main: Path, *, exclude: Path | None = None) -> set[int]:
    """Ports written by the other worktrees of this repo, plus the main checkout's."""
    used: set[int] = {f.main for f in cfg.ports.families}
    for wt in git.list_worktrees(main):
        if exclude and wt.path.resolve() == exclude.resolve():
            continue
        values = envfile.read_worktree(wt.path)
        for family in cfg.ports.families:
            raw = values.get(family.env_key)
            if raw and raw.isdigit():
                used.add(int(raw))
    return used


def branch_ports(
    cfg: WtxConfig,
    main: Path,
    repo_name: str,
    branch: str,
    *,
    exclude: Path | None = None,
) -> dict[str, int]:
    """One port per family for this branch. Keys are the family env keys."""
    families = cfg.ports.families
    if not families:
        return {}
    taken = ports_in_use_by_others(cfg, main, exclude=exclude) | listening_ports()
    h = cksum(f"{repo_name}/{branch}".encode()) % cfg.ports.slots
    for step in range(cfg.ports.slots):
        offset = (h + step) % cfg.ports.slots
        candidate = {f.env_key: cfg.ports.base_for(i) + offset for i, f in enumerate(families)}
        if not (set(candidate.values()) & taken):
            return candidate
    # Every slot is taken. Hand back the plain hash and let the servers complain.
    offset = h
    return {f.env_key: cfg.ports.base_for(i) + offset for i, f in enumerate(families)}


def free_ports(values: dict[str, int]) -> None:
    """Kill whatever holds these ports. Used at teardown."""
    from .proc import run

    for port in values.values():
        run(
            ["fuser", "-k", "-n", "tcp", str(port)],
            check=False,
            quiet=True,
        )
