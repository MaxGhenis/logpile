"""One-time migration: replace byte-identical shared copies with APFS clones.

Shared copies written before sync cloned (see ``sync._secure_copy_file``) are
full byte copies of transcripts whose sources usually still exist.  For each
copy that is provably identical to its source, this clones the source into a
temporary sibling, verifies the clone against the copy's hash, and atomically
renames it over the copy.  At every instant each managed file is either the
old full copy or a verified clone, so the run can be interrupted anywhere.

The command never writes the database and never touches sources, rows whose
source is gone (the shared copy is the sole survivor), or paths outside the
managed shared and private-archive roots.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import apfs
from .parsers import file_hash
from .sync import (
    StorageSafetyError,
    _clone_to_temporary_sibling,
    _discard_temporary,
    _format_copy_volume,
    _is_within,
    _lexical_path,
    _private_archive_root,
    _secure_managed_mkdir,
    sync_lock,
    sync_lock_path,
)

# Skip reasons, in report order.
OUTSIDE_MANAGED_ROOT = "outside_managed_root"
DUPLICATE_SHARED_PATH = "duplicate_shared_path"
UNSAFE_ANCESTRY = "unsafe_ancestry"
SHARED_MISSING = "shared_missing"
SHARED_NOT_REGULAR = "shared_not_regular"
SOURCE_MISSING = "source_missing"
SOURCE_NOT_REGULAR = "source_not_regular"
SAME_FILE = "same_file"
CROSS_VOLUME = "cross_volume"
SIZE_MISMATCH = "size_mismatch"
ALREADY_SHARED = "already_shared"
HASH_MISMATCH = "hash_mismatch"
CLONE_UNSUPPORTED = "clone_unsupported"
CLONE_CHANGED = "clone_changed"
SHARED_CHANGED = "shared_changed"
ERROR = "error"

SKIP_REASONS = (
    OUTSIDE_MANAGED_ROOT,
    DUPLICATE_SHARED_PATH,
    UNSAFE_ANCESTRY,
    SHARED_MISSING,
    SHARED_NOT_REGULAR,
    SOURCE_MISSING,
    SOURCE_NOT_REGULAR,
    SAME_FILE,
    CROSS_VOLUME,
    SIZE_MISMATCH,
    ALREADY_SHARED,
    HASH_MISMATCH,
    CLONE_UNSUPPORTED,
    CLONE_CHANGED,
    SHARED_CHANGED,
    ERROR,
)

SKIP_DESCRIPTIONS = {
    OUTSIDE_MANAGED_ROOT: "shared_path outside the managed roots",
    DUPLICATE_SHARED_PATH: "another row already covered this shared_path",
    UNSAFE_ANCESTRY: "symlink or non-directory between the root and the copy",
    SHARED_MISSING: "shared copy is missing",
    SHARED_NOT_REGULAR: "shared copy is a symlink or not a regular file",
    SOURCE_MISSING: "source is gone; the shared copy is the sole survivor",
    SOURCE_NOT_REGULAR: "source is a symlink or not a regular file",
    SAME_FILE: "shared copy and source are the same inode",
    CROSS_VOLUME: "shared copy and source are on different volumes",
    SIZE_MISMATCH: "sizes differ",
    ALREADY_SHARED: "APFS reports no private blocks; nothing to reclaim",
    HASH_MISMATCH: "same size but different sha256",
    CLONE_UNSUPPORTED: "clonefile(2) unavailable or declined for this path",
    CLONE_CHANGED: "source changed between hashing and cloning",
    SHARED_CHANGED: "shared copy changed during the run",
    ERROR: "I/O or safety error; the copy was left as it was",
}


class RecloneLockContended(RuntimeError):
    """Another process holds the sync lock; nothing was examined or changed."""

    def __init__(self, lock_path: Path) -> None:
        super().__init__(
            f"Another logpile sync or reclone-shared holds {lock_path}; "
            "nothing was examined or changed. Retry once it finishes."
        )
        self.lock_path = lock_path


@dataclass
class RecloneReport:
    """Counts for one run.  Every examined row is either recloned (in a dry
    run: would be) or skipped for exactly one reason, so
    ``examined == recloned + skipped_total``."""

    applied: bool
    db_path: Path
    shared_dir: Path
    private_root: Path
    clone_available: bool
    free_before: int | None = None
    free_after: int | None = None
    examined: int = 0
    examined_by_root: Counter = field(default_factory=Counter)
    identical: int = 0
    identical_bytes: int = 0
    # Dry run: what --apply would reclone.  Apply: what it recloned.
    recloned: int = 0
    recloned_bytes: int = 0
    # APFS private bytes of those shared copies: the blocks a reclone frees.
    reclaim_bytes: int = 0
    reclaim_estimated: bool = False
    skipped: Counter = field(default_factory=Counter)
    skipped_bytes: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())


@dataclass
class _Candidate:
    shared: Path
    source: Path
    root: Path
    shared_stat: os.stat_result
    shared_sha256: str
    reclaim_bytes: int


def _free_bytes(path: Path) -> int | None:
    """Free bytes available to this user at path, as ``df`` reports them."""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _load_rows(db_path: Path) -> list[sqlite3.Row]:
    """Read shared paths through a read-only connection; the DB is never written."""
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT session_id, source_path, shared_path
            FROM sessions
            WHERE shared_path IS NOT NULL AND shared_path != ''
            ORDER BY shared_path
            """
        ).fetchall()
    finally:
        conn.close()


def _ancestry_is_safe(directory: Path, root: Path) -> bool:
    """Read-only twin of _secure_managed_mkdir: every component is a real directory."""
    try:
        relative = directory.relative_to(root)
    except ValueError:
        return False
    current = root
    for component in (None, *relative.parts):
        if component is not None:
            current = current / component
        try:
            if not stat.S_ISDIR(current.lstat().st_mode):
                return False
        except OSError:
            return False
    return True


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


class _Recloner:
    def __init__(
        self,
        report: RecloneReport,
        *,
        log: Callable[[str], None] | None,
    ) -> None:
        self.report = report
        self.log = log
        self.seen: set[Path] = set()

    def skip(self, reason: str, path: Path, size: int = 0, detail: str = "") -> None:
        self.report.skipped[reason] += 1
        self.report.skipped_bytes[reason] += size
        if reason == ERROR:
            self.report.errors.append(f"{path}: {detail}")
        if self.log is not None:
            suffix = f" ({detail})" if detail else ""
            self.log(f"skip {reason}: {path}{suffix}")

    def process(self, row: sqlite3.Row, *, apply: bool) -> None:
        report = self.report
        shared = _lexical_path(Path(row["shared_path"]))
        report.examined += 1
        if _is_within(shared, report.shared_dir):
            root = report.shared_dir
            report.examined_by_root["shared"] += 1
        elif _is_within(shared, report.private_root):
            root = report.private_root
            report.examined_by_root["private"] += 1
        else:
            report.examined_by_root["outside"] += 1
            self.skip(OUTSIDE_MANAGED_ROOT, shared)
            return
        if shared in self.seen:
            self.skip(DUPLICATE_SHARED_PATH, shared)
            return
        self.seen.add(shared)

        try:
            candidate = self.examine(shared, Path(row["source_path"]), root)
        except (OSError, StorageSafetyError) as exc:
            self.skip(ERROR, shared, detail=str(exc))
            return
        if candidate is None:
            return

        size = candidate.shared_stat.st_size
        report.identical += 1
        report.identical_bytes += size
        if not apply:
            report.recloned += 1
            report.recloned_bytes += size
            report.reclaim_bytes += candidate.reclaim_bytes
            if self.log is not None:
                self.log(f"would reclone: {shared}")
            return
        self.reclone(candidate)

    def examine(self, shared: Path, source: Path, root: Path) -> _Candidate | None:
        """Return a verified-identical candidate, or record why it was skipped."""
        if shared == root or not _ancestry_is_safe(shared.parent, root):
            self.skip(UNSAFE_ANCESTRY, shared)
            return None
        try:
            shared_stat = shared.lstat()
        except FileNotFoundError:
            self.skip(SHARED_MISSING, shared)
            return None
        if not stat.S_ISREG(shared_stat.st_mode):
            self.skip(SHARED_NOT_REGULAR, shared)
            return None
        size = shared_stat.st_size

        try:
            source_stat = source.lstat()
        except FileNotFoundError:
            self.skip(SOURCE_MISSING, shared, size)
            return None
        if not stat.S_ISREG(source_stat.st_mode):
            self.skip(SOURCE_NOT_REGULAR, shared, size)
            return None
        if (shared_stat.st_dev, shared_stat.st_ino) == (
            source_stat.st_dev,
            source_stat.st_ino,
        ):
            self.skip(SAME_FILE, shared, size)
            return None
        if shared_stat.st_dev != source_stat.st_dev:
            self.skip(CROSS_VOLUME, shared, size)
            return None
        if size != source_stat.st_size:
            self.skip(SIZE_MISMATCH, shared, size)
            return None

        private = apfs.private_size(shared)
        if private == 0:
            self.skip(ALREADY_SHARED, shared, size)
            return None
        if private is None:
            # No APFS accounting: allocated blocks are the best upper bound.
            private = shared_stat.st_blocks * 512
            self.report.reclaim_estimated = True

        shared_sha256 = file_hash(shared)
        if file_hash(source) != shared_sha256:
            self.skip(HASH_MISMATCH, shared, size)
            return None
        return _Candidate(
            shared=shared,
            source=source,
            root=root,
            shared_stat=shared_stat,
            shared_sha256=shared_sha256,
            reclaim_bytes=private,
        )

    def reclone(self, candidate: _Candidate) -> None:
        report = self.report
        shared = candidate.shared
        size = candidate.shared_stat.st_size
        try:
            label = (
                "shared storage"
                if candidate.root == report.shared_dir
                else "private archive"
            )
            # Re-secure (0700, no symlink components) before the clone, whose
            # inherited source mode must never be visible to other users.
            _secure_managed_mkdir(shared.parent, candidate.root, label=label)
            clone = _clone_to_temporary_sibling(candidate.source, shared)
        except (OSError, StorageSafetyError) as exc:
            self.skip(ERROR, shared, size, str(exc))
            return
        if clone is None:
            self.skip(CLONE_UNSUPPORTED, shared, size)
            return

        try:
            if file_hash(clone) != candidate.shared_sha256:
                _discard_temporary(clone)
                self.skip(CLONE_CHANGED, shared, size)
                return
            try:
                current = shared.lstat()
            except FileNotFoundError:
                current = None
            if current is None or _identity(current) != _identity(
                candidate.shared_stat
            ):
                _discard_temporary(clone)
                self.skip(SHARED_CHANGED, shared, size)
                return
            os.replace(clone, shared)
        except OSError as exc:
            _discard_temporary(clone)
            self.skip(ERROR, shared, size, str(exc))
            return
        except BaseException:
            _discard_temporary(clone)
            raise

        report.recloned += 1
        report.recloned_bytes += size
        report.reclaim_bytes += candidate.reclaim_bytes
        if self.log is not None:
            self.log(f"recloned: {shared}")


def reclone_shared_copies(
    db_path: Path,
    shared_dir: Path,
    *,
    apply: bool = False,
    progress: Callable[[int, int], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> RecloneReport:
    """Examine every managed shared copy and, with ``apply``, reclone it.

    Holds the exclusive sync lock for the whole run, so a concurrent
    ``logpile sync`` skips instead of rewriting copies underneath it.  Raises
    RecloneLockContended when the lock is already held, and SyncLockError
    when the lock itself is unsafe.
    """
    db_path = _lexical_path(db_path)
    shared_dir = _lexical_path(shared_dir)
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")

    with sync_lock(db_path) as acquired:
        if not acquired:
            raise RecloneLockContended(sync_lock_path(db_path))
        report = RecloneReport(
            applied=apply,
            db_path=db_path,
            shared_dir=shared_dir,
            private_root=_private_archive_root(shared_dir),
            clone_available=apfs.clone_available(),
        )
        report.free_before = _free_bytes(shared_dir)
        rows = _load_rows(db_path)
        recloner = _Recloner(report, log=log)
        for index, row in enumerate(rows, start=1):
            recloner.process(row, apply=apply)
            if progress is not None:
                progress(index, len(rows))
        report.free_after = _free_bytes(shared_dir)
    return report


def _files(count: int) -> str:
    return f"{count:,} file" if count == 1 else f"{count:,} files"


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    return f"{_format_copy_volume(value)} ({value:,} bytes)"


def format_report(report: RecloneReport) -> list[str]:
    mode = (
        "APPLY"
        if report.applied
        else "DRY RUN: nothing was changed; re-run with --apply"
    )
    lines = [
        f"logpile reclone-shared ({mode})",
        f"Database:      {report.db_path}",
        f"Shared root:   {report.shared_dir}",
        f"Private root:  {report.private_root}",
        f"Free space before: {_format_bytes(report.free_before)}",
    ]
    if not report.clone_available:
        lines.append(
            "clonefile(2) is unavailable on this platform; --apply would skip "
            "every identical copy as clone_unsupported."
        )
    by_root = report.examined_by_root
    lines.append(
        f"Examined: {report.examined:,} shared copies "
        f"(shared root {by_root['shared']:,}, private archive {by_root['private']:,}, "
        f"outside managed roots {by_root['outside']:,})"
    )
    lines.append(
        f"Identical to source: {report.identical:,} "
        f"({_format_copy_volume(report.identical_bytes)})"
    )
    verb = "Recloned" if report.applied else "Would reclone"
    reclaim_note = (
        "allocated-size estimate" if report.reclaim_estimated else "APFS private bytes"
    )
    lines.append(
        f"{verb}: {_files(report.recloned)}, "
        f"{_format_copy_volume(report.recloned_bytes)} logical, "
        f"{_format_copy_volume(report.reclaim_bytes)} reclaimable ({reclaim_note})"
    )
    lines.append(f"Skipped: {report.skipped_total:,}")
    for reason in SKIP_REASONS:
        count = report.skipped.get(reason, 0)
        if not count:
            continue
        lines.append(
            f"  {reason:<22} {count:>9,}  "
            f"{_format_copy_volume(report.skipped_bytes.get(reason, 0)):>10}  "
            f"{SKIP_DESCRIPTIONS[reason]}"
        )
    for message in report.errors[:20]:
        lines.append(f"  error: {message}")
    if len(report.errors) > 20:
        lines.append(f"  … {len(report.errors) - 20:,} more errors")
    lines.append(f"Free space after:  {_format_bytes(report.free_after)}")
    if not report.applied and report.free_before is not None:
        lines.append(
            "Projected free space after --apply: "
            f"{_format_bytes(report.free_before + report.reclaim_bytes)}"
        )
    if report.applied and report.recloned:
        lines.append(
            "Blocks still referenced by APFS snapshots (for example Time Machine "
            "local snapshots) are freed only when those snapshots expire."
        )
    return lines
