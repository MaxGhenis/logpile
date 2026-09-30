"""Free-space guard that defers sync on a nearly full volume.

A sync rewrites database pages and replaces archival clones. On APFS, every
overwritten block is copy-on-write, and a Time Machine local snapshot keeps
the old block alive until the snapshot expires, so page churn consumes real
free space even though the database does not grow. The guard refuses to start
(or stops early) when the volume is below a floor, and raises the floor while
a local snapshot exists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

GIB = 1024**3
DEFAULT_MIN_FREE_GIB = 40.0
DEFAULT_MIN_FREE_WITH_SNAPSHOT_GIB = 60.0
_SNAPSHOT_PREFIX = "com.apple.TimeMachine."


@dataclass(frozen=True)
class DiskGuardPolicy:
    min_free_bytes: int = int(DEFAULT_MIN_FREE_GIB * GIB)
    min_free_with_snapshot_bytes: int = int(DEFAULT_MIN_FREE_WITH_SNAPSHOT_GIB * GIB)
    enabled: bool = True

    @classmethod
    def from_gib(
        cls,
        min_free_gib: float = DEFAULT_MIN_FREE_GIB,
        min_free_with_snapshot_gib: float = DEFAULT_MIN_FREE_WITH_SNAPSHOT_GIB,
        *,
        enabled: bool = True,
    ) -> DiskGuardPolicy:
        if min_free_gib < 0 or min_free_with_snapshot_gib < 0:
            raise ValueError("free-space floors must be non-negative")
        return cls(
            min_free_bytes=int(min_free_gib * GIB),
            min_free_with_snapshot_bytes=int(min_free_with_snapshot_gib * GIB),
            enabled=enabled,
        )

    @classmethod
    def disabled(cls) -> DiskGuardPolicy:
        return cls(enabled=False)


@dataclass(frozen=True)
class DiskGuardDecision:
    ok: bool
    path: Path | None
    free_bytes: int | None
    floor_bytes: int
    snapshot_count: int | None
    reason: str


def _format_gib(value: int) -> str:
    return f"{value / GIB:.1f} GiB"


def list_local_snapshots() -> tuple[str, ...] | None:
    """Time Machine local snapshot names, or None when they cannot be listed.

    Local snapshots live on the macOS data volume; ``tmutil`` reports them
    for ``/``. Other platforms have no Time Machine, so they report None
    (unknown) and only the plain floor applies.
    """
    if sys.platform != "darwin":
        return None
    tmutil = shutil.which("tmutil") or "/usr/bin/tmutil"
    try:
        completed = subprocess.run(
            [tmutil, "listlocalsnapshots", "/"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return tuple(
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip().startswith(_SNAPSHOT_PREFIX)
    )


def _existing_ancestor(path: Path) -> Path:
    candidate = Path(path)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def check_disk_space(
    paths: Iterable[Path],
    policy: DiskGuardPolicy,
    *,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
    snapshot_lister: Callable[[], tuple[str, ...] | None] = list_local_snapshots,
    snapshots: tuple[str, ...] | None = None,
    snapshots_known: bool = False,
) -> DiskGuardDecision:
    """Decide whether sync may write to the volumes holding ``paths``.

    The tightest volume decides. ``snapshots``/``snapshots_known`` let a
    long-running caller reuse one ``tmutil`` answer instead of forking it on
    every re-check.
    """
    if not policy.enabled:
        return DiskGuardDecision(
            ok=True,
            path=None,
            free_bytes=None,
            floor_bytes=0,
            snapshot_count=None,
            reason="disk guard disabled",
        )

    tightest: tuple[int, Path] | None = None
    seen_devices: set[int] = set()
    for raw_path in paths:
        path = _existing_ancestor(Path(raw_path))
        try:
            device = os.stat(path).st_dev
        except OSError:
            device = None
        if device is not None:
            if device in seen_devices:
                continue
            seen_devices.add(device)
        free = int(disk_usage(path).free)
        if tightest is None or free < tightest[0]:
            tightest = (free, path)
    if tightest is None:
        raise ValueError("check_disk_space needs at least one path")
    free_bytes, path = tightest

    if not snapshots_known:
        snapshots = snapshot_lister()
    snapshot_count = None if snapshots is None else len(snapshots)
    floor = policy.min_free_bytes
    floor_label = "free-space floor"
    if snapshot_count:
        floor = max(floor, policy.min_free_with_snapshot_bytes)
        floor_label = (
            f"free-space floor while {snapshot_count} Time Machine local "
            "snapshot(s) retain overwritten blocks"
        )
    if free_bytes < floor:
        return DiskGuardDecision(
            ok=False,
            path=path,
            free_bytes=free_bytes,
            floor_bytes=floor,
            snapshot_count=snapshot_count,
            reason=(
                f"{_format_gib(free_bytes)} free at {path} is below the "
                f"{_format_gib(floor)} {floor_label}"
            ),
        )
    return DiskGuardDecision(
        ok=True,
        path=path,
        free_bytes=free_bytes,
        floor_bytes=floor,
        snapshot_count=snapshot_count,
        reason=(
            f"{_format_gib(free_bytes)} free at {path} (floor {_format_gib(floor)})"
        ),
    )
