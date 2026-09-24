"""One-time migration: replace byte-identical shared copies with APFS clones.

Shared copies written before sync cloned (see ``sync._secure_copy_file``) are
full byte copies of transcripts whose sources usually still exist.  For each
copy that is provably identical to its source, this clones the source into a
temporary sibling, verifies the clone against the copy's hash, and atomically
swaps it into place.  At every instant each managed file is either the old
full copy or a verified clone, so the run can be interrupted anywhere.

A copy already cloned from its source is recognised by its APFS clone id
(``apfs.FileStorage.is_clone_of``), never by its private size: any APFS
snapshot, such as an hourly Time Machine local snapshot, traps the blocks of
every older byte copy and drops its private size to 0.

The command never writes the database file and never touches sources, rows
whose source is gone (the shared copy is the sole survivor), or paths outside
the managed shared and private-archive roots.  Inside those roots it also
removes staging files that a killed copy left behind, but only ones that are
byte-identical to the copy they were staged for.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import apfs
from .parsers import file_hash
from .sync import (
    STAGING_NAME_PATTERN,
    StorageSafetyError,
    _clone_allowed_by_flags,
    _clone_to_temporary_sibling,
    _discard_temporary,
    _format_copy_volume,
    _is_within,
    _lexical_path,
    _lexists,
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
HARD_LINKED = "hard_linked"
SOURCE_MISSING = "source_missing"
SOURCE_NOT_REGULAR = "source_not_regular"
SAME_FILE = "same_file"
CROSS_VOLUME = "cross_volume"
SIZE_MISMATCH = "size_mismatch"
ALREADY_CLONED = "already_cloned"
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
    HARD_LINKED,
    SOURCE_MISSING,
    SOURCE_NOT_REGULAR,
    SAME_FILE,
    CROSS_VOLUME,
    SIZE_MISMATCH,
    ALREADY_CLONED,
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
    HARD_LINKED: "shared copy has other hard links; recloning would free nothing",
    SOURCE_MISSING: "source is gone; the shared copy is the sole survivor",
    SOURCE_NOT_REGULAR: "source is a symlink or not a regular file",
    SAME_FILE: "shared copy and source are the same inode",
    CROSS_VOLUME: "shared copy and source are on different volumes",
    SIZE_MISMATCH: "sizes differ",
    ALREADY_CLONED: "already a clone of its source (same APFS clone id)",
    HASH_MISMATCH: "same size but different sha256",
    CLONE_UNSUPPORTED: "clonefile(2) unavailable or declined for this path",
    CLONE_CHANGED: "source changed between hashing and cloning",
    SHARED_CHANGED: "shared copy changed or moved during the run",
    ERROR: "I/O or safety error; the copy was left as it was",
}

# Stale staging files: why one was left in place, in report order.
STAGING_RECENT = "recent"
STAGING_NOT_REGULAR = "not_regular"
STAGING_OTHER_OWNER = "other_owner"
STAGING_UNSAFE_ANCESTRY = "unsafe_ancestry"
STAGING_NO_COPY = "no_published_copy"
STAGING_DIFFERS = "differs"
STAGING_ERROR = "error"

# A live staging file exists for milliseconds (a clone) to seconds (a byte
# copy), and its ctime is refreshed as it is written.  Visibility transitions
# stage copies without the sync lock, so anything newer than this is left
# alone in case it belongs to one that is still running.
STAGING_MIN_AGE_SECONDS = 600

STAGING_KEPT_REASONS = (
    STAGING_RECENT,
    STAGING_NOT_REGULAR,
    STAGING_OTHER_OWNER,
    STAGING_UNSAFE_ANCESTRY,
    STAGING_NO_COPY,
    STAGING_DIFFERS,
    STAGING_ERROR,
)

STAGING_KEPT_DESCRIPTIONS = {
    STAGING_RECENT: (
        f"changed in the last {STAGING_MIN_AGE_SECONDS // 60} minutes; "
        "may belong to a running copy"
    ),
    STAGING_NOT_REGULAR: "a symlink, directory, or other non-regular file",
    STAGING_OTHER_OWNER: "owned by another user",
    STAGING_UNSAFE_ANCESTRY: "symlink or non-directory between the root and it",
    STAGING_NO_COPY: "no regular published copy beside it to compare with",
    STAGING_DIFFERS: "bytes differ from the published copy beside it",
    STAGING_ERROR: "could not be read or removed",
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
    # APFS private bytes of those old copies: freed as soon as they are gone.
    reclaim_now_bytes: int = 0
    reclaim_now_estimated: bool = False
    # Their allocated bytes: the most a reclone can free, once no APFS
    # snapshot (or other clone) still holds the old copies' blocks.
    reclaim_eventual_bytes: int = 0
    skipped: Counter = field(default_factory=Counter)
    skipped_bytes: Counter = field(default_factory=Counter)
    # Stale staging files byte-identical to their published copy.  Dry run:
    # would remove.  Apply: removed.
    staging_removed: int = 0
    staging_removed_bytes: int = 0
    staging_kept: Counter = field(default_factory=Counter)
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
    reclaim_now: int
    reclaim_eventual: int


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
    """Read shared paths through a read-only connection.

    The database file itself is never written.  In WAL mode SQLite may still
    create the empty ``-wal`` and ``-shm`` sidecars if they are missing.
    """
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


def _is_real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _swapped_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    # A rename changes the renamed file's ctime, so compare everything else.
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _inode(value: os.stat_result) -> tuple[int, int]:
    return (value.st_dev, value.st_ino)


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

    def _log(self, message: str) -> None:
        if self.log is not None:
            self.log(message)

    def skip(self, reason: str, path: Path, size: int = 0, detail: str = "") -> None:
        self.report.skipped[reason] += 1
        self.report.skipped_bytes[reason] += size
        if reason == ERROR:
            self.report.errors.append(f"{path}: {detail}")
        suffix = f" ({detail})" if detail else ""
        self._log(f"skip {reason}: {path}{suffix}")

    # -- stale staging files ------------------------------------------------

    def sweep_staging(self, root: Path, *, apply: bool) -> None:
        """Find staging files a killed copy left under root; remove safe ones.

        A staging file is removed only when it is a regular file this user
        owns, older than STAGING_MIN_AGE_SECONDS, under a symlink-free
        ancestry, and byte-identical to the published copy beside it.  That
        covers both leftovers a killed reclone can produce (the verified
        clone before the swap, the displaced old copy after it) and never
        deletes bytes that exist nowhere else.
        """
        if not _is_real_directory(root):
            return
        now = time.time()
        for dirpath, dirnames, filenames in os.walk(root):
            directory = Path(dirpath)
            for name in list(dirnames):
                if STAGING_NAME_PATTERN.match(name):
                    # A directory clone: report it, never descend or remove.
                    dirnames.remove(name)
                    self._keep_staging(STAGING_NOT_REGULAR, directory / name)
            for name in filenames:
                match = STAGING_NAME_PATTERN.match(name)
                if match is not None:
                    self._consider_staging(
                        directory / name,
                        directory / match.group("name"),
                        root,
                        now=now,
                        apply=apply,
                    )

    def _keep_staging(self, reason: str, path: Path, detail: str = "") -> None:
        self.report.staging_kept[reason] += 1
        if reason == STAGING_ERROR:
            self.report.errors.append(f"{path}: {detail}")
        suffix = f" ({detail})" if detail else ""
        self._log(f"staging file kept, {reason}: {path}{suffix}")

    def _consider_staging(
        self, path: Path, published: Path, root: Path, *, now: float, apply: bool
    ) -> None:
        try:
            staged = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            self._keep_staging(STAGING_ERROR, path, str(exc))
            return
        if not stat.S_ISREG(staged.st_mode):
            self._keep_staging(STAGING_NOT_REGULAR, path)
            return
        if staged.st_uid != os.geteuid():
            self._keep_staging(STAGING_OTHER_OWNER, path)
            return
        if now - staged.st_ctime < STAGING_MIN_AGE_SECONDS:
            self._keep_staging(STAGING_RECENT, path)
            return
        if not _ancestry_is_safe(path.parent, root):
            self._keep_staging(STAGING_UNSAFE_ANCESTRY, path)
            return
        try:
            copy = published.lstat()
        except FileNotFoundError:
            self._keep_staging(STAGING_NO_COPY, path)
            return
        except OSError as exc:
            self._keep_staging(STAGING_ERROR, path, str(exc))
            return
        if not stat.S_ISREG(copy.st_mode):
            self._keep_staging(STAGING_NO_COPY, path)
            return
        try:
            identical = copy.st_size == staged.st_size and file_hash(path) == file_hash(
                published
            )
        except OSError as exc:
            self._keep_staging(STAGING_ERROR, path, str(exc))
            return
        if not identical:
            self._keep_staging(STAGING_DIFFERS, path)
            return
        if apply:
            _discard_temporary(path)
            if _lexists(path):
                self._keep_staging(STAGING_ERROR, path, "could not remove it")
                return
            self._log(f"removed stale staging file: {path}")
        else:
            self._log(f"would remove stale staging file: {path}")
        self.report.staging_removed += 1
        self.report.staging_removed_bytes += staged.st_size

    # -- shared copies ------------------------------------------------------

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
            self._count_recloned(candidate)
            self._log(f"would reclone: {shared}")
            return
        if self.reclone(candidate):
            self._count_recloned(candidate)
            self._log(f"recloned: {shared}")

    def _count_recloned(self, candidate: _Candidate) -> None:
        report = self.report
        report.recloned += 1
        report.recloned_bytes += candidate.shared_stat.st_size
        report.reclaim_now_bytes += candidate.reclaim_now
        report.reclaim_eventual_bytes += candidate.reclaim_eventual

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
        if shared_stat.st_nlink > 1:
            # Another name keeps the old inode, so replacing this one frees
            # nothing and would split the link.
            self.skip(HARD_LINKED, shared, size)
            return None

        try:
            source_stat = source.lstat()
        except FileNotFoundError:
            self.skip(SOURCE_MISSING, shared, size)
            return None
        if not stat.S_ISREG(source_stat.st_mode):
            self.skip(SOURCE_NOT_REGULAR, shared, size)
            return None
        if _inode(shared_stat) == _inode(source_stat):
            self.skip(SAME_FILE, shared, size)
            return None

        shared_storage = apfs.file_storage(shared) or apfs.FileStorage()
        source_storage = apfs.file_storage(source) or apfs.FileStorage()
        # st_dev alone misses the sealed system volume, which reports the
        # data volume's st_dev but refuses clones with EXDEV.
        if shared_stat.st_dev != source_stat.st_dev or (
            shared_storage.fsid is not None
            and source_storage.fsid is not None
            and shared_storage.fsid != source_storage.fsid
        ):
            self.skip(CROSS_VOLUME, shared, size)
            return None
        if size != source_stat.st_size:
            self.skip(SIZE_MISMATCH, shared, size)
            return None
        if shared_storage.is_clone_of(source_storage):
            self.skip(ALREADY_CLONED, shared, size)
            return None
        if not _clone_allowed_by_flags(source_stat):
            self.skip(CLONE_UNSUPPORTED, shared, size, "source file flags")
            return None

        allocated = shared_stat.st_blocks * 512
        reclaim_now = shared_storage.private_size
        if reclaim_now is None:
            # No APFS accounting: allocated blocks are the best upper bound.
            reclaim_now = allocated
            self.report.reclaim_now_estimated = True

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
            reclaim_now=reclaim_now,
            reclaim_eventual=allocated,
        )

    def reclone(self, candidate: _Candidate) -> bool:
        """Swap a verified clone of the source over the copy; True on success."""
        shared = candidate.shared
        size = candidate.shared_stat.st_size
        try:
            label = (
                "shared storage"
                if candidate.root == self.report.shared_dir
                else "private archive"
            )
            # Re-secure (0700, no symlink components) before the clone, whose
            # inherited source mode must never be visible to other users.
            _secure_managed_mkdir(shared.parent, candidate.root, label=label)
            clone = _clone_to_temporary_sibling(candidate.source, shared)
        except (OSError, StorageSafetyError) as exc:
            self.skip(ERROR, shared, size, str(exc))
            return False
        if clone is None:
            self.skip(CLONE_UNSUPPORTED, shared, size)
            return False

        try:
            ours = _inode(clone.lstat())
            if file_hash(clone) != candidate.shared_sha256:
                _discard_temporary(clone)
                self.skip(CLONE_CHANGED, shared, size)
                return False
            try:
                current = shared.lstat()
            except FileNotFoundError:
                current = None
            if current is None or _identity(current) != _identity(
                candidate.shared_stat
            ):
                _discard_temporary(clone)
                self.skip(SHARED_CHANGED, shared, size)
                return False
        except OSError as exc:
            _discard_temporary(clone)
            self.skip(ERROR, shared, size, str(exc))
            return False
        except BaseException:
            _discard_temporary(clone)
            raise

        try:
            reason, detail = self._publish(clone, ours, candidate)
        except OSError as exc:
            self.skip(ERROR, shared, size, str(exc))
            return False
        if reason is not None:
            self.skip(reason, shared, size, detail)
            return False
        return True

    def _publish(
        self, clone: Path, ours: tuple[int, int], candidate: _Candidate
    ) -> tuple[str | None, str]:
        """Atomically put the verified clone where the old copy is.

        rename_swap exchanges the two names, so it can neither recreate a copy
        that a visibility transition moved away after the identity check (it
        fails with ENOENT instead) nor silently discard a file that replaced
        the copy (the swapped-out file is checked, and swapped back if it is
        not the copy that was hashed).  Where swapping is unsupported, this
        falls back to os.replace.

        However this returns or raises, the staging name is tidied last: it is
        removed only while it holds our clone or the displaced old copy.
        """
        shared = candidate.shared
        old = _inode(candidate.shared_stat)
        try:
            try:
                apfs.rename_swap(clone, shared)
            except apfs.SwapUnsupported:
                os.replace(clone, shared)
                return None, ""
            except FileNotFoundError:
                return SHARED_CHANGED, "moved away before the swap"
            displaced = clone.lstat()
            if _swapped_identity(displaced) == _swapped_identity(candidate.shared_stat):
                return None, ""
            # Replaced after the identity check: give that file its name back.
            apfs.rename_swap(clone, shared)
            return SHARED_CHANGED, "replaced before the swap; restored"
        finally:
            self._tidy(clone, ours, old, shared)

    def _tidy(
        self,
        clone: Path,
        ours: tuple[int, int],
        old: tuple[int, int],
        shared: Path,
    ) -> None:
        try:
            left = clone.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            self.report.errors.append(f"{clone}: cannot inspect staging file: {exc}")
            return
        if _inode(left) not in (ours, old):
            self.report.errors.append(
                f"{clone}: left in place; it holds a file that replaced "
                f"{shared} during the swap"
            )
            return
        _discard_temporary(clone)
        if _lexists(clone):
            self.report.errors.append(f"{clone}: could not remove staging file")


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
        recloner = _Recloner(report, log=log)
        for root in (report.shared_dir, report.private_root):
            recloner.sweep_staging(root, apply=apply)
        rows = _load_rows(db_path)
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
    lines.append(
        f"{verb}: {_files(report.recloned)}, "
        f"{_format_copy_volume(report.recloned_bytes)} logical"
    )
    now_note = (
        "allocated-size estimate; APFS reported no private size"
        if report.reclaim_now_estimated
        else "APFS private bytes"
    )
    lines.append(
        f"  Frees now:    {_format_bytes(report.reclaim_now_bytes)} ({now_note})"
    )
    lines.append(
        f"  Frees up to:  {_format_bytes(report.reclaim_eventual_bytes)} "
        "(allocated bytes, once no snapshot or other clone holds the old copies)"
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
    kept_total = sum(report.staging_kept.values())
    if report.staging_removed or kept_total:
        staged_verb = "removed" if report.applied else "would remove"
        lines.append(
            f"Stale staging files (*.tmp-sync): {staged_verb} "
            f"{report.staging_removed:,} "
            f"({_format_copy_volume(report.staging_removed_bytes)}), "
            f"left in place {kept_total:,}"
        )
        for reason in STAGING_KEPT_REASONS:
            count = report.staging_kept.get(reason, 0)
            if count:
                lines.append(
                    f"  {reason:<22} {count:>9,}  {STAGING_KEPT_DESCRIPTIONS[reason]}"
                )
    for message in report.errors[:20]:
        lines.append(f"  error: {message}")
    if len(report.errors) > 20:
        lines.append(f"  … {len(report.errors) - 20:,} more errors")
    lines.append(f"Free space after:  {_format_bytes(report.free_after)}")
    if not report.applied and report.free_before is not None:
        lines.append(
            "Projected free space after --apply: "
            f"{_format_bytes(report.free_before + report.reclaim_now_bytes)} at "
            "once, up to "
            f"{_format_bytes(report.free_before + report.reclaim_eventual_bytes)}"
        )
    held = report.reclaim_eventual_bytes - report.reclaim_now_bytes
    if report.recloned and held > 0:
        lines.append(
            f"{_format_copy_volume(held)} of the old copies' blocks are shared "
            "with an APFS snapshot (for example a Time Machine local snapshot; "
            "`tmutil listlocalsnapshots /` lists them) or with another clone. "
            "Blocks a snapshot holds are freed only when that snapshot is deleted."
        )
    return lines
