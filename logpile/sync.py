"""Sync JSONL sessions to a shared directory and update the SQLite index."""
import errno
import fcntl
import fnmatch
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .discovery import claude_projects_root, codex_session_roots, discover_transcripts
from .parsers import (
    PrivateSessionMarker,
    file_hash,
    parse_claudecode_session,
    parse_codex_session,
)
from .objectives import SESSION_OBJECTIVE_VERSION, derive_session_objective
from .origins import SESSION_ORIGIN_VERSION, derive_session_origin
from .db import (
    apply_message_claims,
    defer_storage_transition,
    defer_storage_transitions,
    ensure_user,
    get_meta,
    get_user_by_identifier,
    get_db,
    init_db,
    insert_session_daily_usage,
    insert_session_paths,
    insert_tool_calls,
    normalize_username,
    refresh_session_publication_metadata,
    refresh_native_usage,
    resolve_session_visibility,
    set_meta,
    transition_session_visibility,
    upsert_session,
)


SESSION_ACTIVITY_VERSION = 1
SESSION_NARRATIVE_VERSION = 1
SESSION_IDENTITY_VERSION = 1
# 4: codex replay-burst deltas (resume snapshots no longer re-count history),
#    Claude cache_creation capture, per-day usage rows, file size/mtime.
# 5: cross-session Claude dedup — parses emit message_claims and native_*
#    columns are claims-derived (db.CLAIMS_TOKEN_VERSION).
# 6: ISO-8601 timestamps are normalized to UTC before daily usage bucketing.
# 7: structural Codex replay/reset accounting, explicit residual daily usage,
#    and exact Claude cache-creation subtype accounting.
SESSION_TOKEN_VERSION = 7


class SyncStatus(str, Enum):
    COMPLETED = "completed"
    LOCK_CONTENDED = "lock_contended"


class SyncResult(tuple):
    """Tuple-compatible sync counts with a machine-readable completion status."""

    status = SyncStatus.COMPLETED

    def __new__(cls, new: int, updated: int, skipped: int):
        return super().__new__(cls, (new, updated, skipped))

    @property
    def new(self) -> int:
        return self[0]

    @property
    def updated(self) -> int:
        return self[1]

    @property
    def skipped(self) -> int:
        return self[2]


class SyncLockContended(SyncResult):
    status = SyncStatus.LOCK_CONTENDED


class SyncLockError(RuntimeError):
    """The platform or filesystem could not provide the required sync lock."""


class StorageSafetyError(RuntimeError):
    """A storage transition could not preserve the only durable transcript."""


@dataclass
class PrivateStorageTransition:
    """Filesystem changes that can be finalized or reversed around a DB update."""

    archive_path: Path
    changed: bool = False
    rollback_moves: list[tuple[Path, Path]] = field(default_factory=list)
    rollback_unlinks: list[Path] = field(default_factory=list)
    commit_unlinks: list[Path] = field(default_factory=list)
    rollback_quarantine_root: Path | None = None
    active: bool = True

    def rollback(self) -> None:
        if not self.active:
            return
        errors: list[OSError] = []
        for path in reversed(self.rollback_unlinks):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                quarantined = False
                if self.rollback_quarantine_root is not None and _lexists(path):
                    try:
                        quarantine_dir = (
                            self.rollback_quarantine_root / ".rollback-quarantine"
                        )
                        _secure_private_mkdir(
                            quarantine_dir, self.rollback_quarantine_root
                        )
                        quarantine = _temporary_sibling(
                            quarantine_dir / path.name, "rollback"
                        )
                        os.replace(path, quarantine)
                        os.chmod(quarantine, 0o600)
                        quarantined = True
                    except OSError as quarantine_exc:
                        errors.append(quarantine_exc)
                    except StorageSafetyError as quarantine_exc:
                        errors.append(OSError(str(quarantine_exc)))
                if not quarantined:
                    errors.append(exc)
        for current, original in reversed(self.rollback_moves):
            try:
                if current.exists() or current.is_symlink():
                    _secure_mkdir(original.parent)
                    os.replace(current, original)
            except OSError as exc:
                errors.append(exc)
        self.active = False
        if errors:
            raise StorageSafetyError(
                f"Could not roll back private transcript storage: {errors[0]}"
            ) from errors[0]

    def commit(self) -> None:
        if not self.active:
            return
        for path in self.commit_unlinks:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # Quarantine files are already private and outside the shared
                # tree. A later cleanup is preferable to rolling visibility
                # back after the durable archive has been created.
                pass
        self.active = False
_TEST_PATTERNS = (
    re.compile(r"\b(pytest|py\.test|vitest|jest|nosetests|rspec)\b"),
    re.compile(r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+(pytest|unittest)\b"),
    re.compile(r"\b(go|deno|mix)\s+test\b"),
    re.compile(r"\bcargo\s+test\b"),
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b"),
    re.compile(r"\buv\s+run\s+pytest\b"),
)
_LINT_PATTERNS = (
    re.compile(r"\b(?:ruff(?:\s+check)?|flake8|pylint|eslint|shellcheck|hadolint|mypy|pyright)\b"),
    re.compile(r"\bbiome\s+check\b"),
    re.compile(r"\bgolangci-lint\b"),
    re.compile(r"\btsc\b.*--noemit\b"),
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?lint\b"),
)
_BUILD_PATTERNS = (
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build\b"),
    re.compile(r"\b(?:next|vite|webpack|rollup|parcel)\s+build\b"),
    re.compile(r"\bcargo\s+build\b"),
    re.compile(r"\bgo\s+build\b"),
    re.compile(r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+build\b"),
    re.compile(r"\btsc\b(?!.*--noemit\b)"),
)
_FORMAT_PATTERNS = (
    re.compile(r"\b(?:prettier|black|isort|gofmt|rustfmt|clang-format|stylua)\b"),
    re.compile(r"\bruff\s+format\b"),
    re.compile(r"\bbiome\s+format\b"),
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?format\b"),
)


def _safe_expanduser(path: Path) -> Path:
    try:
        return path.expanduser()
    except RuntimeError:
        return path


def _secure_mkdir(path: Path, *, harden_existing: bool = True) -> None:
    """Create private runtime directories without umask-readable intermediates."""
    path = Path(path)
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    if harden_existing and path.exists() and not path.is_symlink():
        path.chmod(0o700)


def _harden_regular_file(path: Path) -> bool:
    """Set an existing regular file to 0600 without following symlinks.

    O_NONBLOCK prevents an attacker-controlled FIFO replacement from hanging
    sync between the lstat and open.  The descriptor check closes the race
    before fchmod.
    """
    path = Path(path)
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return False
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        os.fchmod(fd, 0o600)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _harden_managed_artifact(path: Path, root: Path) -> bool:
    """Harden a regular artifact and its managed directory ancestry."""
    path = _lexical_path(path)
    root = _lexical_path(root)
    try:
        relative_parent = path.parent.relative_to(root)
    except ValueError:
        return False

    directories = [root]
    current = root
    for component in relative_parent.parts:
        current /= component
        directories.append(current)
    for directory in directories:
        try:
            mode = directory.lstat().st_mode
            if not stat.S_ISDIR(mode):
                return False
            directory.chmod(0o700)
        except OSError:
            return False
    return _harden_regular_file(path)


def _lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _is_within(path: Path, root: Path) -> bool:
    try:
        _lexical_path(path).relative_to(_lexical_path(root))
        return True
    except ValueError:
        return False


def _secure_managed_mkdir(path: Path, root: Path, *, label: str) -> None:
    """Create a managed directory without traversing symlink components."""
    path = _lexical_path(path)
    root = _lexical_path(root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise StorageSafetyError(
            f"{label.title()} path escapes its configured root: {path}"
        ) from exc

    current = root
    components = (Path(), *relative.parts)
    for component in components:
        if component != Path():
            current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
                mode = current.lstat().st_mode
            except FileExistsError:
                # A concurrent replacement won the create race; validate it
                # with lstat below instead of following it.
                mode = current.lstat().st_mode
        except OSError as exc:
            raise StorageSafetyError(
                f"Cannot inspect {label} directory {current}: {exc}"
            ) from exc

        if not stat.S_ISDIR(mode):
            kind = "symlink" if stat.S_ISLNK(mode) else "non-directory"
            raise StorageSafetyError(
                f"Refusing {kind} {label} component: {current}"
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(current, flags)
            try:
                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise StorageSafetyError(
                        f"Refusing replaced {label} component: {current}"
                    )
                os.fchmod(fd, 0o700)
            finally:
                os.close(fd)
        except StorageSafetyError:
            raise
        except OSError as exc:
            raise StorageSafetyError(
                f"Cannot secure {label} directory {current}: {exc}"
            ) from exc


def _secure_private_mkdir(path: Path, private_root: Path) -> None:
    _secure_managed_mkdir(path, private_root, label="private archive")


def _secure_shared_mkdir(path: Path, shared_root: Path) -> None:
    _secure_managed_mkdir(path, shared_root, label="shared storage")


def _validate_private_archive_file(path: Path, private_root: Path) -> None:
    """Validate the archive ancestry and reject non-regular archive leaves."""
    path = _lexical_path(path)
    _secure_private_mkdir(path.parent, private_root)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StorageSafetyError(
            f"Cannot inspect private archive artifact {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(mode):
        kind = "symlink" if stat.S_ISLNK(mode) else "non-regular"
        raise StorageSafetyError(
            f"Refusing {kind} private archive artifact: {path}"
        )


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _private_archive_root(shared_dir: Path) -> Path:
    shared_dir = _lexical_path(shared_dir)
    return shared_dir.parent / f".{shared_dir.name}-private"


def _storage_component(value: str | None, fallback: str) -> str:
    component = Path((value or "").strip()).name
    return component if component not in {"", ".", ".."} else fallback


def _private_archive_path(
    shared_dir: Path,
    username: str,
    source: str,
    project: str,
    filename: str,
) -> Path:
    return (
        _private_archive_root(shared_dir)
        / _storage_component(username, "user")
        / _storage_component(source, "unknown")
        / _storage_component(project, "unknown")
        / _storage_component(filename, "session.jsonl")
    )


def _temporary_sibling(path: Path, suffix: str) -> Path:
    for index in range(1000):
        candidate = path.with_name(
            f".{path.name}.{os.getpid()}.{index}.{suffix}"
        )
        if not _lexists(candidate):
            return candidate
    raise StorageSafetyError(f"Could not reserve temporary storage beside {path}")


def _private_quarantine_path(
    private_root: Path, original: Path, suffix: str
) -> Path:
    """Reserve a rollback path outside the shared/public storage tree."""
    quarantine_dir = private_root / ".transition-quarantine"
    _secure_private_mkdir(quarantine_dir, private_root)
    return _temporary_sibling(quarantine_dir / original.name, suffix)


def _secure_copy_file(
    src: Path,
    dst: Path,
    *,
    private_root: Path | None = None,
    shared_root: Path | None = None,
) -> None:
    """Copy bytes to a 0600 staging file and atomically replace lexical dst."""
    if private_root is not None and shared_root is not None:
        raise ValueError("A copy destination cannot have two managed roots")
    if private_root is not None:
        _validate_private_archive_file(dst, private_root)
    elif shared_root is not None:
        _secure_shared_mkdir(dst.parent, shared_root)
    else:
        _secure_mkdir(dst.parent)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{dst.name}.", suffix=".tmp-sync", dir=dst.parent
    )
    tmp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with src.open("rb") as source_file, os.fdopen(fd, "wb") as target_file:
            fd = -1
            shutil.copyfileobj(source_file, target_file)
            target_file.flush()
            os.fsync(target_file.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _unchanged_on_disk(
    existing_row,
    jsonl_path: Path,
    shared_dir: Path,
    *,
    preflight_source: tuple[int, float, str] | None = None,
) -> bool:
    """Cheap no-op check for a synced session: same path, same size+mtime,
    and the shared copy (when one is expected) still present.

    file_hash() reads the whole file, and _copy_session hashes both sides
    again — tens of GB per sync once immutable archives are scanned. Any
    mismatch here just falls through to the full hash-and-parse path.
    """
    if "copy_retry_pending" in existing_row.keys() and existing_row["copy_retry_pending"]:
        return False
    # Full SHA-256 replaced the legacy 16-character prefix.  Force one
    # verified reparse/copy so old rows cannot remain on the cheap fast path.
    if len(existing_row["file_hash"] or "") != 64:
        return False
    if existing_row["file_size"] is None or existing_row["file_mtime"] is None:
        return False
    if existing_row["source_path"] != str(jsonl_path):
        return False
    try:
        stat = jsonl_path.stat()
    except OSError:
        return False
    if existing_row["file_size"] != stat.st_size:
        return False
    if abs(existing_row["file_mtime"] - stat.st_mtime) > 1e-6:
        return False
    # Published revisions get a streaming content check even when an attacker
    # or restore operation preserves size+mtime.  Public bytes stay frozen in
    # the reviewed artifact, but any source drift must still be requeued.
    if existing_row["visibility"] == "public":
        try:
            source_hash = (
                preflight_source[2]
                if preflight_source is not None
                and preflight_source[0] == stat.st_size
                and abs(preflight_source[1] - stat.st_mtime) <= 1e-6
                else file_hash(jsonl_path)
            )
            if source_hash != existing_row["file_hash"]:
                return False
        except OSError:
            return False
    shared_path = existing_row["shared_path"] or ""
    if existing_row["visibility"] == "private":
        if not shared_path:
            # Older rows could predate private archival copies.  Treat the
            # missing managed artifact as repair work so preflight accounts
            # for its volume and the normal copy-and-verify path heals it.
            return False
        return _harden_managed_artifact(
            Path(shared_path), _private_archive_root(shared_dir)
        )
    if not shared_path:
        return False
    return _harden_managed_artifact(Path(shared_path), shared_dir)


def _record_copy_retry(
    conn,
    *,
    source_path: Path,
    session_id: str,
    expected_sha256: str,
    file_stat,
    error: BaseException,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_copy_retries (
            source_path, session_id, expected_sha256, expected_size,
            expected_mtime, last_error, attempted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_path) DO UPDATE SET
            session_id = excluded.session_id,
            expected_sha256 = excluded.expected_sha256,
            expected_size = excluded.expected_size,
            expected_mtime = excluded.expected_mtime,
            last_error = excluded.last_error,
            attempted_at = excluded.attempted_at
        """,
        (
            str(source_path),
            session_id,
            expected_sha256,
            file_stat.st_size,
            file_stat.st_mtime,
            str(error),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    print(
        f"Warning: archival copy for {source_path} was not verified; "
        "source hash/mtime were not advanced and sync will retry.",
        file=sys.stderr,
    )


def _clear_copy_retry(conn, source_path: Path, session_id: str) -> None:
    conn.execute(
        "DELETE FROM sync_copy_retries WHERE source_path = ? OR session_id = ?",
        (str(source_path), session_id),
    )


def _resolve_sync_username(conn, requested_username: str) -> str:
    direct = get_user_by_identifier(conn, requested_username)
    if direct:
        return direct["username"]

    normalized = normalize_username(requested_username)
    direct = get_user_by_identifier(conn, normalized)
    if direct:
        return direct["username"]

    return normalized


def _copy_session(src: Path, dst: Path) -> bool:
    """Copy a session file into the shared directory.

    Atomic: writes to a 0600 temp file beside dst, then os.replace()s it into
    place, so the shared copy is always a complete regular file (old or new).
    ENOSPC and all other write errors fail closed; no source-pointing symlink
    is ever created.
    """
    _secure_shared_mkdir(dst.parent, dst.parent)
    if _harden_regular_file(dst) and file_hash(dst) == file_hash(src):
        return False
    _secure_copy_file(src, dst, shared_root=dst.parent)
    return True


def _prepare_private_storage(
    *,
    src: Path,
    shared_dir: Path,
    username: str,
    source: str,
    project: str,
    filename: str,
    existing_shared_path: str | None,
) -> PrivateStorageTransition:
    """Prepare a reversible move from shared storage into private storage."""
    shared_dir = _lexical_path(shared_dir)
    _secure_shared_mkdir(shared_dir, shared_dir)
    private_root = _private_archive_root(shared_dir)
    _secure_private_mkdir(private_root, private_root)
    desired_shared = _desired_shared_path(
        shared_dir, username, source, project, filename
    )
    archive = _private_archive_path(
        shared_dir, username, source, project, filename
    )

    existing_path = Path(existing_shared_path) if existing_shared_path else None
    if existing_path and _is_within(existing_path, private_root):
        archive = _lexical_path(existing_path)
    elif existing_path and _lexists(existing_path) and not _is_within(
        existing_path, shared_dir
    ):
        raise StorageSafetyError(
            f"Refusing to privatize transcript outside the configured shared tree: {existing_path}"
        )
    _validate_private_archive_file(archive, private_root)

    shared_candidates: list[Path] = []
    for candidate in (existing_path, desired_shared):
        if not candidate or not _is_within(candidate, shared_dir):
            continue
        lexical = _lexical_path(candidate)
        if lexical not in shared_candidates and _lexists(lexical):
            if not _harden_managed_artifact(lexical, shared_dir):
                raise StorageSafetyError(
                    f"Refusing non-regular or unsafe shared transcript: {lexical}"
                )
            shared_candidates.append(lexical)

    transition = PrivateStorageTransition(archive_path=archive)
    try:
        if src.exists():
            if _lexists(archive):
                archive_backup = _temporary_sibling(archive, "rollback")
                os.replace(archive, archive_backup)
                transition.rollback_moves.append((archive_backup, archive))
                transition.commit_unlinks.append(archive_backup)
            _secure_copy_file(src, archive, private_root=private_root)
            transition.rollback_unlinks.append(archive)
            transition.changed = True

            for shared_path in shared_candidates:
                quarantine = _temporary_sibling(archive, "shared-rollback")
                os.replace(shared_path, quarantine)
                transition.rollback_moves.append((quarantine, shared_path))
                transition.commit_unlinks.append(quarantine)
                transition.changed = True
        elif _lexists(archive):
            for shared_path in shared_candidates:
                quarantine = _temporary_sibling(archive, "shared-rollback")
                os.replace(shared_path, quarantine)
                transition.rollback_moves.append((quarantine, shared_path))
                transition.commit_unlinks.append(quarantine)
                transition.changed = True
        elif len(shared_candidates) == 1:
            shared_path = shared_candidates[0]
            if shared_path.is_symlink():
                raise StorageSafetyError(
                    "Cannot preserve a source-pointing shared symlink after its source disappeared."
                )
            _secure_private_mkdir(archive.parent, private_root)
            os.replace(shared_path, archive)
            transition.rollback_moves.append((archive, shared_path))
            transition.changed = True
        elif len(shared_candidates) > 1:
            raise StorageSafetyError(
                "Refusing private transition with multiple surviving shared transcripts."
            )
        else:
            raise StorageSafetyError(
                "Cannot make session private: no source or shared transcript survives."
            )
    except BaseException:
        transition.rollback()
        raise
    return transition


def prepare_private_session_storage(
    row,
    *,
    shared_dir: Path,
) -> PrivateStorageTransition:
    src = Path(row["source_path"])
    shared_path = row["shared_path"] or ""
    filename = src.name or (Path(shared_path).name if shared_path else "session.jsonl")
    return _prepare_private_storage(
        src=src,
        shared_dir=shared_dir,
        username=row["username"],
        source=row["source"],
        project=row["project"] or "unknown",
        filename=filename,
        existing_shared_path=shared_path or None,
    )


def _prepare_shared_storage(
    *,
    src: Path,
    shared_dir: Path,
    username: str,
    source: str,
    project: str,
    filename: str,
    existing_shared_path: str | None,
) -> PrivateStorageTransition:
    """Prepare a reversible materialization into the lexical shared tree."""
    shared_dir = _lexical_path(shared_dir)
    _secure_shared_mkdir(shared_dir, shared_dir)
    desired = _desired_shared_path(
        shared_dir, username, source, project, filename
    )
    _secure_shared_mkdir(desired.parent, shared_dir)
    existing = Path(existing_shared_path) if existing_shared_path else None
    private_root = _private_archive_root(shared_dir)
    _secure_private_mkdir(private_root, private_root)
    if existing and _lexists(existing) and not (
        _is_within(existing, shared_dir)
        or _is_within(existing, private_root)
    ):
        raise StorageSafetyError(
            f"Refusing to publish transcript from outside managed storage: {existing}"
        )

    copy_src = src
    if existing and _is_within(existing, private_root):
        _validate_private_archive_file(existing, private_root)
    elif existing and _is_within(existing, shared_dir) and _lexists(existing):
        if not _harden_managed_artifact(existing, shared_dir):
            raise StorageSafetyError(
                f"Refusing non-regular or unsafe shared transcript: {existing}"
            )
    if (
        not copy_src.exists()
        and existing
        and _is_within(existing, private_root)
        and _lexists(existing)
    ):
        copy_src = existing
    if not copy_src.exists():
        raise StorageSafetyError(
            "Cannot make session non-private: no source or private archive survives."
        )

    transition = PrivateStorageTransition(
        archive_path=desired,
        rollback_quarantine_root=private_root,
    )
    try:
        same_lexical_path = _lexical_path(copy_src) == _lexical_path(desired)
        identical = (
            not same_lexical_path
            and _harden_managed_artifact(desired, shared_dir)
            and file_hash(desired) == file_hash(copy_src)
        )
        if same_lexical_path and not _harden_managed_artifact(desired, shared_dir):
            raise StorageSafetyError(
                f"Managed shared transcript is not a regular file: {desired}"
            )
        if not same_lexical_path and not identical:
            if _lexists(desired):
                backup = _private_quarantine_path(
                    private_root, desired, "publish-rollback"
                )
                os.replace(desired, backup)
                transition.rollback_moves.append((backup, desired))
                transition.commit_unlinks.append(backup)
            _secure_copy_file(copy_src, desired, shared_root=shared_dir)
            transition.rollback_unlinks.append(desired)
            transition.changed = True

        if existing and _lexical_path(existing) != _lexical_path(desired) and _lexists(existing):
            if _is_within(existing, private_root):
                _validate_private_archive_file(existing, private_root)
                quarantine = _temporary_sibling(existing, "publish-rollback")
                os.replace(existing, quarantine)
                transition.rollback_moves.append((quarantine, existing))
                transition.commit_unlinks.append(quarantine)
            else:
                # A managed shared artifact being renamed within the shared
                # tree should also remain reversible until the DB commits.
                quarantine = _private_quarantine_path(
                    private_root, existing, "shared-rollback"
                )
                os.replace(existing, quarantine)
                transition.rollback_moves.append((quarantine, existing))
                transition.commit_unlinks.append(quarantine)
            transition.changed = True
    except BaseException:
        transition.rollback()
        raise
    return transition


def prepare_shared_session_storage(
    row,
    *,
    shared_dir: Path,
) -> PrivateStorageTransition:
    src = Path(row["source_path"])
    existing = row["shared_path"] or ""
    filename = src.name or (Path(existing).name if existing else "session.jsonl")
    return _prepare_shared_storage(
        src=src,
        shared_dir=shared_dir,
        username=row["username"],
        source=row["source"],
        project=row["project"] or "unknown",
        filename=filename,
        existing_shared_path=existing or None,
    )


def _remove_shared_copy(path: Path | None) -> bool:
    if path is None:
        return False
    if not path.exists() and not path.is_symlink():
        return False
    path.unlink()
    return True


def _desired_shared_path(
    shared_dir: Path,
    username: str,
    source: str,
    project: str,
    filename: str,
) -> Path:
    return (
        shared_dir
        / _storage_component(username, "user")
        / _storage_component(source, "unknown")
        / _storage_component(project, "unknown")
        / _storage_component(filename, "session.jsonl")
    )


def _prepare_sync_shared_copy(
    *,
    src: Path,
    shared_dir: Path,
    username: str,
    source: str,
    project: str,
    filename: str,
    visibility: str,
    existing_shared_path: str | None = None,
) -> PrivateStorageTransition:
    if visibility == "private":
        return _prepare_private_storage(
            src=src,
            shared_dir=shared_dir,
            username=username,
            source=source,
            project=project,
            filename=filename,
            existing_shared_path=existing_shared_path,
        )
    return _prepare_shared_storage(
        src=src,
        shared_dir=shared_dir,
        username=username,
        source=source,
        project=project,
        filename=filename,
        existing_shared_path=existing_shared_path,
    )


def _sync_shared_copy(
    *,
    conn,
    src: Path,
    shared_dir: Path,
    username: str,
    source: str,
    project: str,
    filename: str,
    visibility: str,
    expected_sha256: str,
    existing_shared_path: str | None = None,
) -> tuple[str, bool]:
    transition = _prepare_sync_shared_copy(
        src=src,
        shared_dir=shared_dir,
        username=username,
        source=source,
        project=project,
        filename=filename,
        visibility=visibility,
        existing_shared_path=existing_shared_path,
    )
    copied_path = transition.archive_path
    managed_root = (
        _private_archive_root(shared_dir) if visibility == "private" else shared_dir
    )
    try:
        if not _harden_managed_artifact(copied_path, managed_root):
            raise OSError(
                errno.EIO,
                f"archival copy is not a safe regular file: {copied_path}",
            )
        copied_sha256 = file_hash(copied_path)
        if copied_sha256 != expected_sha256:
            raise OSError(
                errno.EIO,
                "archival copy hash mismatch; source metadata was not advanced",
            )
    except BaseException:
        transition.rollback()
        raise
    defer_storage_transition(conn, transition)
    return str(transition.archive_path), transition.changed


def _effective_storage_visibility(
    existing_row,
    resolved_visibility: dict[str, str | int | None],
) -> str:
    if existing_row and (existing_row["visibility_source"] or "default") == "manual":
        return existing_row["visibility"] or resolved_visibility["visibility"]
    return resolved_visibility["visibility"]


def reconcile_session_storage(
    conn,
    *,
    shared_dir: Path,
    session_id: str | None = None,
    session_id_prefix: str | None = None,
    username: str | None = None,
) -> int:
    clauses: list[str] = []
    params: list[str] = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if session_id_prefix:
        clauses.append("session_id LIKE ?")
        params.append(f"{session_id_prefix}%")
    if username:
        clauses.append("username = ?")
        params.append(username)
    where = " AND ".join(clauses) if clauses else "1 = 1"

    rows = conn.execute(
        f"""
        SELECT
            session_id,
            username,
            source,
            project,
            source_path,
            shared_path,
            visibility
        FROM sessions
        WHERE {where}
        """,
        params,
    ).fetchall()

    updated = 0
    transitions: list[PrivateStorageTransition] = []
    try:
        for row in rows:
            src = Path(row["source_path"])
            current_shared_path = row["shared_path"] or ""
            transition: PrivateStorageTransition | None = None

            if row["visibility"] == "private":
                transition = _prepare_sync_shared_copy(
                    src=src,
                    shared_dir=shared_dir,
                    username=row["username"],
                    source=row["source"],
                    project=row["project"] or "unknown",
                    filename=src.name,
                    visibility="private",
                    existing_shared_path=current_shared_path or None,
                )
            elif src.exists() or (
                current_shared_path
                and _is_within(
                    Path(current_shared_path), _private_archive_root(shared_dir)
                )
                and _lexists(Path(current_shared_path))
            ):
                transition = _prepare_sync_shared_copy(
                    src=src,
                    shared_dir=shared_dir,
                    username=row["username"],
                    source=row["source"],
                    project=row["project"] or "unknown",
                    filename=src.name,
                    visibility=row["visibility"],
                    existing_shared_path=current_shared_path or None,
                )

            if transition is None:
                continue
            transitions.append(transition)
            next_shared_path = str(transition.archive_path)
            if transition.changed or next_shared_path != current_shared_path:
                conn.execute(
                    "UPDATE sessions SET shared_path = ? WHERE session_id = ?",
                    (next_shared_path, row["session_id"]),
                )
                updated += 1
    except BaseException as operation_error:
        first_rollback_error: BaseException | None = None
        for transition in reversed(transitions):
            try:
                transition.rollback()
            except BaseException as rollback_error:
                if first_rollback_error is None:
                    first_rollback_error = rollback_error
        if first_rollback_error is not None:
            raise first_rollback_error from operation_error
        raise

    defer_storage_transitions(conn, transitions)
    return updated


def load_ignore_patterns(home: Path) -> list[str]:
    patterns = []
    for name in (".logpile-ignore", ".agentus-ignore"):
        ignore_file = home / name
        if not ignore_file.exists():
            continue
        with open(ignore_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns


def should_ignore(path: Path, patterns: list[str]) -> bool:
    name = str(path)
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(path.name, pattern):
            return True
    return False


def _format_copy_volume(value: int) -> str:
    amount = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    return f"{amount:.1f} {unit}"


def _preflight_shared_copy_volume(
    *,
    home: Path,
    shared_dir: Path,
    existing: dict[str, object],
    patterns: list[str],
) -> tuple[int, int, dict[str, tuple[int, float, str]]]:
    """Plan conservative archival-copy volume before mutating storage."""
    planned_bytes = 0
    planned_files = 0
    public_source_hashes: dict[str, tuple[int, float, str]] = {}
    seen_session_ids: set[tuple[str, str]] = set()
    for discovered in discover_transcripts(home):
        jsonl_path = discovered.path
        if should_ignore(jsonl_path, patterns):
            continue
        if discovered.source == "claudecode" and _is_claude_workflow_journal(jsonl_path):
            continue
        session_key = (discovered.source, jsonl_path.stem)
        if session_key in seen_session_ids:
            continue
        seen_session_ids.add(session_key)
        row = existing.get(jsonl_path.stem)
        preflight_source = None
        if row is not None and row["visibility"] == "public":
            try:
                source_stat = jsonl_path.stat()
                preflight_source = (
                    source_stat.st_size,
                    source_stat.st_mtime,
                    file_hash(jsonl_path),
                )
                public_source_hashes[str(jsonl_path)] = preflight_source
            except OSError:
                preflight_source = None
        if row is not None and _unchanged_on_disk(
            row,
            jsonl_path,
            shared_dir,
            preflight_source=preflight_source,
        ):
            continue
        try:
            size = jsonl_path.stat().st_size
        except OSError:
            continue
        planned_bytes += max(0, size)
        planned_files += 1

    if not planned_files:
        return 0, 0, public_source_hashes
    free_bytes = shutil.disk_usage(shared_dir).free
    message = (
        "Archival shared-copy preflight plans "
        f"{_format_copy_volume(planned_bytes)} across {planned_files} transcript(s); "
        f"{_format_copy_volume(free_bytes)} free at {shared_dir}."
    )
    print(f"Warning: {message}", file=sys.stderr)
    if planned_bytes > free_bytes:
        raise StorageSafetyError(
            f"{message} Not enough free space; no transcript copies were started."
        )
    return planned_bytes, planned_files, public_source_hashes


def project_from_claude_path(jsonl_path: Path) -> str:
    """
    Claude encodes project paths as directory names like:
      -Users-maxghenis-PolicyEngine-policyengine-us
    We decode and return the leaf directory name.
    """
    # A sidechain lives below
    #   <encoded-project>/<root-session>/subagents/<agent>.jsonl
    # (and some workflow journals nest more deeply below ``subagents``).
    # The immediate parent is therefore not the encoded project directory.
    parts = jsonl_path.parts
    if "subagents" in parts:
        subagents_index = len(parts) - 1 - tuple(reversed(parts)).index("subagents")
        project_index = subagents_index - 2
        dir_name = parts[project_index] if project_index >= 0 else jsonl_path.parent.name
    else:
        dir_name = jsonl_path.parent.name
    if dir_name.startswith("-"):
        dir_name = dir_name[1:]
    # Claude replaces '/' with '-', but so does '-' in repo names, so just take last segment
    parts = dir_name.split("-")
    # Return the last non-empty, non-UUID-looking part
    for part in reversed(parts):
        if part and len(part) > 1:
            return part
    return dir_name or "unknown"


def _is_claude_workflow_journal(jsonl_path: Path) -> bool:
    """Claude workflow progress journals are not standalone transcripts.

    They repeat the same agentId as the full ``agent-*.jsonl`` transcript and
    contain only started/result progress records. Indexing both would make the
    zero-usage journal overwrite the real agent row; every journal is also
    literally named ``journal.jsonl``, so archived copies would collide.
    """
    return (
        jsonl_path.name == "journal.jsonl"
        and "subagents" in jsonl_path.parts
        and "workflows" in jsonl_path.parts
    )


def _git_output(workspace: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    output = result.stdout.strip()
    return output or None


def _resolve_repo_metadata(
    workspace_root: str | None,
    cache: dict[str, dict[str, str | int | None]],
) -> dict[str, str | int | None]:
    key = (workspace_root or "").strip()
    if key in cache:
        return cache[key]

    if not key or key == "unknown":
        cache[key] = {
            "worktree_root": None,
            "repo_root": None,
            "repo_name": None,
            "git_branch": None,
            "git_commit": None,
            "git_dirty": 0,
        }
        return cache[key]

    workspace = _safe_expanduser(Path(key))
    if workspace.exists():
        try:
            workspace = workspace.resolve()
        except OSError:
            pass
    fallback_worktree_root = str(workspace)
    fallback_repo_name = workspace.name or None
    metadata: dict[str, str | int | None] = {
        "worktree_root": fallback_worktree_root,
        "repo_root": None,
        "repo_name": fallback_repo_name,
        "git_branch": None,
        "git_commit": None,
        "git_dirty": 0,
    }
    if not workspace.exists():
        cache[key] = metadata
        return metadata

    worktree_root = _git_output(
        workspace,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
    )
    if not worktree_root:
        cache[key] = metadata
        return metadata

    common_dir = _git_output(
        workspace,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    repo_root = worktree_root
    if common_dir:
        common_path = Path(common_dir)
        if common_path.name == ".git":
            repo_root = str(common_path.parent)

    git_branch = _git_output(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if git_branch == "HEAD":
        git_branch = None

    metadata = {
        "worktree_root": worktree_root,
        "repo_root": repo_root,
        "repo_name": Path(repo_root).name or Path(worktree_root).name or fallback_repo_name,
        "git_branch": git_branch,
        "git_commit": _git_output(workspace, "rev-parse", "HEAD"),
        "git_dirty": 1 if _git_output(workspace, "status", "--porcelain") else 0,
    }
    cache[key] = metadata
    return metadata


def _normalize_workspace_root(workspace_root: str | None) -> str | None:
    value = (workspace_root or "").strip()
    if not value or value == "unknown":
        return None
    path = _safe_expanduser(Path(value))
    if path.exists():
        try:
            path = path.resolve()
        except OSError:
            pass
    return str(path)


def _repo_relative_path(normalized_path: str | None, root: str | None) -> str | None:
    if not normalized_path or not root:
        return None
    # A path captured from tool-call args can contain a null byte (or other
    # garbage); such a string can't be a real repo-relative path, and
    # Path.resolve() raises ValueError on it. Skip it rather than crash sync.
    if "\x00" in normalized_path or "\x00" in root:
        return None
    path = _safe_expanduser(Path(normalized_path))
    root_path = _safe_expanduser(Path(root))
    try:
        path = path.resolve(strict=False)
    except (OSError, ValueError):
        pass
    try:
        root_path = root_path.resolve(strict=False)
    except (OSError, ValueError):
        pass
    try:
        relative = str(path.relative_to(root_path))
    except ValueError:
        return None
    return relative if relative not in {"", "."} else None


class _AnnotatedSessionPaths:
    """Reusable lazy view that adds repo-relative paths without a list."""

    def __init__(
        self,
        session_paths,
        canonical_root: str | None,
    ) -> None:
        self._session_paths = session_paths
        self._canonical_root = canonical_root

    def __iter__(self):
        for session_path in self._session_paths:
            yield replace(
                session_path,
                repo_relative_path=_repo_relative_path(
                    session_path.normalized_path,
                    self._canonical_root,
                ),
            )

    def __len__(self) -> int:
        return len(self._session_paths)


def _annotate_session_paths(
    session_paths,
    *,
    repo_root: str | None,
    worktree_root: str | None,
    workspace_root: str | None,
) -> _AnnotatedSessionPaths:
    canonical_root = worktree_root or workspace_root
    return _AnnotatedSessionPaths(session_paths, canonical_root)


def _matches_any(command: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(command) for pattern in patterns)


def _derive_session_activity(tool_calls, session_paths) -> dict[str, int]:
    metrics = {
        "write_path_count": 0,
        "read_path_count": 0,
        "search_path_count": 0,
        "test_run_count": 0,
        "test_failure_count": 0,
        "lint_run_count": 0,
        "lint_failure_count": 0,
        "build_run_count": 0,
        "build_failure_count": 0,
        "format_run_count": 0,
        "format_failure_count": 0,
        "git_status_count": 0,
        "git_diff_count": 0,
        "git_commit_count": 0,
        "activity_version": SESSION_ACTIVITY_VERSION,
    }

    if len(session_paths):
        with tempfile.TemporaryDirectory(prefix="logpile-path-counts-") as td:
            count_db_path = Path(td) / "paths.sqlite"
            fd = os.open(
                count_db_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(fd)
            count_db = sqlite3.connect(count_db_path)
            try:
                count_db.execute("PRAGMA journal_mode = OFF")
                count_db.execute("PRAGMA synchronous = OFF")
                count_db.execute("PRAGMA temp_store = FILE")
                count_db.execute("PRAGMA cache_size = -1024")
                count_db.execute("PRAGMA mmap_size = 0")
                count_db.execute(
                    """
                    CREATE TABLE unique_paths (
                        operation TEXT NOT NULL,
                        normalized_path TEXT NOT NULL,
                        PRIMARY KEY (operation, normalized_path)
                    ) WITHOUT ROWID
                    """
                )
                count_db.executemany(
                    "INSERT OR IGNORE INTO unique_paths VALUES (?, ?)",
                    (
                        (path.operation, path.normalized_path)
                        for path in session_paths
                        if path.operation in {"write", "read", "search"}
                    ),
                )
                for operation, count in count_db.execute(
                    "SELECT operation, COUNT(*) FROM unique_paths GROUP BY operation"
                ):
                    metrics[f"{operation}_path_count"] = int(count)
            finally:
                count_db.close()

    for tool_call in tool_calls:
        command = (tool_call.command or "").strip().lower()
        if not command:
            continue

        is_test = _matches_any(command, _TEST_PATTERNS)
        is_lint = _matches_any(command, _LINT_PATTERNS)
        is_build = _matches_any(command, _BUILD_PATTERNS)
        is_format = _matches_any(command, _FORMAT_PATTERNS)

        if is_test:
            metrics["test_run_count"] += 1
            if tool_call.is_error:
                metrics["test_failure_count"] += 1
        if is_lint:
            metrics["lint_run_count"] += 1
            if tool_call.is_error:
                metrics["lint_failure_count"] += 1
        if is_build:
            metrics["build_run_count"] += 1
            if tool_call.is_error:
                metrics["build_failure_count"] += 1
        if is_format:
            metrics["format_run_count"] += 1
            if tool_call.is_error:
                metrics["format_failure_count"] += 1
        if re.search(r"\bgit\s+status\b", command):
            metrics["git_status_count"] += 1
        if re.search(r"\bgit\s+diff\b", command):
            metrics["git_diff_count"] += 1
        if re.search(r"\bgit\s+commit\b", command):
            metrics["git_commit_count"] += 1

    return metrics


def _clip_text(value: str | None, limit: int = 160) -> str | None:
    if not value:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _sample_paths(session_paths: list, limit: int = 2) -> list[str]:
    samples: list[str] = []
    seen: set[str] = set()
    for session_path in session_paths:
        if session_path.operation != "write":
            continue
        label = session_path.repo_relative_path or session_path.relative_path or session_path.display_path
        if not label or label in seen:
            continue
        seen.add(label)
        samples.append(label)
        if len(samples) >= limit:
            break
    return samples


def _status_from_activity(activity: dict[str, int], error_count: int) -> str:
    made_changes = (
        (activity.get("write_path_count") or 0) > 0
        or (activity.get("format_run_count") or 0) > 0
        or (activity.get("git_commit_count") or 0) > 0
    )
    passed_checks = any(
        (activity.get(total_key) or 0) > (activity.get(failure_key) or 0)
        for total_key, failure_key in (
            ("test_run_count", "test_failure_count"),
            ("build_run_count", "build_failure_count"),
            ("lint_run_count", "lint_failure_count"),
        )
    )
    has_failures = (
        error_count > 0
        or (activity.get("test_failure_count") or 0) > 0
        or (activity.get("build_failure_count") or 0) > 0
        or (activity.get("lint_failure_count") or 0) > 0
        or (activity.get("format_failure_count") or 0) > 0
    )
    if has_failures:
        if made_changes or passed_checks:
            return "partial"
        return "failed"
    if made_changes or passed_checks:
        return "success"
    return "exploration"


def _derive_session_narrative(
    *,
    source: str,
    project: str,
    repo_name: str | None,
    first_user_message: str | None,
    error_count: int,
    activity: dict[str, int],
    session_paths: list,
) -> dict[str, str | int | None]:
    goal = _clip_text(first_user_message, limit=180)
    status = _status_from_activity(activity, error_count)

    scope = repo_name or (project if project and project != "unknown" else None) or source
    summary_bits: list[str] = [f"Worked in {scope}."]

    write_count = activity.get("write_path_count") or 0
    if write_count:
        path_samples = _sample_paths(session_paths)
        path_fragment = f" ({', '.join(path_samples)})" if path_samples else ""
        summary_bits.append(f"Touched {write_count} file{'s' if write_count != 1 else ''}{path_fragment}.")

    for label, total_key, failure_key in (
        ("tests", "test_run_count", "test_failure_count"),
        ("lint checks", "lint_run_count", "lint_failure_count"),
        ("builds", "build_run_count", "build_failure_count"),
    ):
        total = activity.get(total_key) or 0
        failures = activity.get(failure_key) or 0
        if not total:
            continue
        fragment = f"Ran {label} {total} time{'s' if total != 1 else ''}"
        if failures:
            fragment += f" with {failures} failure{'s' if failures != 1 else ''}"
        summary_bits.append(f"{fragment}.")

    commit_count = activity.get("git_commit_count") or 0
    if commit_count:
        summary_bits.append(f"Made {commit_count} git commit{'s' if commit_count != 1 else ''}.")
    elif not write_count and not any(
        (activity.get(key) or 0) > 0
        for key in ("test_run_count", "lint_run_count", "build_run_count")
    ):
        summary_bits.append("Primarily inspected files and commands.")

    if status == "success":
        if commit_count:
            outcome = f"Ended with {commit_count} git commit{'s' if commit_count != 1 else ''}."
        elif write_count:
            outcome = "Ended with code changes and no recorded failing checks."
        else:
            outcome = "Completed without recorded failures."
    elif status == "partial":
        failure_parts = []
        for label, key in (
            ("test", "test_failure_count"),
            ("lint", "lint_failure_count"),
            ("build", "build_failure_count"),
        ):
            count = activity.get(key) or 0
            if count:
                failure_parts.append(f"{count} {label} failure{'s' if count != 1 else ''}")
        joined = ", ".join(failure_parts) if failure_parts else "recorded errors"
        outcome = f"Made progress, but left {joined}."
    elif status == "failed":
        failure_parts = []
        for label, key in (
            ("test", "test_failure_count"),
            ("lint", "lint_failure_count"),
            ("build", "build_failure_count"),
        ):
            count = activity.get(key) or 0
            if count:
                failure_parts.append(f"{count} {label} failure{'s' if count != 1 else ''}")
        joined = ", ".join(failure_parts) if failure_parts else "errors"
        outcome = f"Ended with {joined} and no successful publish signal."
    else:
        outcome = "No decisive code-change outcome was recorded."

    return {
        "session_goal": goal,
        "session_summary": " ".join(summary_bits),
        "session_outcome": outcome,
        "session_status": status,
        "narrative_version": SESSION_NARRATIVE_VERSION,
    }


def _compute_duration(t1: str, t2: str) -> float | None:
    if not t1 or not t2:
        return None
    try:
        from datetime import datetime
        d1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
        d2 = datetime.fromisoformat(t2.replace("Z", "+00:00"))
        return max(0.0, (d2 - d1).total_seconds())
    except Exception:
        return None


def _backfill_tokens_from_shared(conn, verbose: bool = False) -> tuple[int, set[str]]:
    """Re-parse stale sessions whose source is gone but shared copy survives.

    Claude Code rotates transcripts after ~30 days and Codex sessions can be
    deleted outright; the ledger keeps their rows. Without this, those rows
    could never pick up parser fixes. Refresh every parser-derived field that
    replay detection can suppress: identity/lineage, timestamps, message and
    tool counts, tool rows, token components, daily usage, and claims. Repo,
    narrative, visibility, and storage metadata remain untouched because they
    depend on sync-time context rather than transcript parsing.

    Returns (backfilled_count, session ids needing a native_* refresh).
    """
    rows = conn.execute(
        """
        SELECT session_id, source, source_path, shared_path
        FROM sessions
        WHERE (
                COALESCE(token_version, 0) < ?
                OR COALESCE(identity_version, 0) < ?
              )
          AND shared_path IS NOT NULL AND shared_path != ''
        """,
        (SESSION_TOKEN_VERSION, SESSION_IDENTITY_VERSION),
    ).fetchall()
    backfilled = 0
    affected: set[str] = set()
    for row in rows:
        if Path(row["source_path"]).exists():
            continue  # live files belong to the main scan loops
        shared_file = Path(row["shared_path"])
        if not shared_file.exists():
            continue
        parser = (
            parse_claudecode_session
            if row["source"] == "claudecode"
            else parse_codex_session
        )
        try:
            info = parser(shared_file)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise
            if verbose:
                print(f"  Skipped unavailable shared copy {shared_file}: {exc}", file=sys.stderr)
            continue
        if info is None or isinstance(info, PrivateSessionMarker):
            continue
        conn.execute(
            """
            UPDATE sessions
            SET first_timestamp = ?,
                last_timestamp = ?,
                duration_seconds = ?,
                user_message_count = ?,
                assistant_message_count = ?,
                tool_call_count = ?,
                error_count = ?,
                total_input_tokens = ?,
                total_output_tokens = ?,
                fresh_input_tokens = ?,
                cached_input_tokens = ?,
                cache_creation_input_tokens = ?,
                cache_creation_5m_input_tokens = ?,
                cache_creation_1h_input_tokens = ?,
                cache_creation_unknown_input_tokens = ?,
                reasoning_output_tokens = ?,
                token_version = ?,
                first_user_message = ?,
                parent_session_id = ?,
                spawn_depth = ?,
                thread_id = ?,
                parent_thread_id = ?,
                identity_version = ?,
                model = COALESCE(?, model)
            WHERE session_id = ?
            """,
            (
                info.first_timestamp,
                info.last_timestamp,
                _compute_duration(info.first_timestamp, info.last_timestamp),
                info.user_message_count,
                info.assistant_message_count,
                info.tool_call_count,
                info.error_count,
                info.total_input_tokens,
                info.total_output_tokens,
                info.fresh_input_tokens,
                info.cached_input_tokens,
                info.cache_creation_input_tokens,
                info.cache_creation_5m_input_tokens,
                info.cache_creation_1h_input_tokens,
                info.cache_creation_unknown_input_tokens,
                info.reasoning_output_tokens,
                SESSION_TOKEN_VERSION,
                info.first_user_message,
                info.parent_session_id if row["source"] == "claudecode" else None,
                info.spawn_depth,
                info.thread_id,
                info.parent_thread_id,
                SESSION_IDENTITY_VERSION,
                info.model,
                row["session_id"],
            ),
        )
        refresh_session_publication_metadata(
            conn,
            row["session_id"],
            reason="rotated transcript metadata drifted from the reviewed revision",
        )
        insert_tool_calls(conn, row["session_id"], info.tool_calls)
        insert_session_daily_usage(conn, row["session_id"], info.daily_usage)
        affected.add(row["session_id"])
        if row["source"] == "claudecode":
            affected |= apply_message_claims(conn, row["session_id"], info.message_usage)
        backfilled += 1
        if backfilled % 200 == 0:
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if verbose:
            print(f"  Backfilled parsed state from shared copy: {row['session_id'][:20]}…")
    return backfilled, affected


def _resolve_canonical_parents(conn) -> None:
    """Resolve raw thread parentage to exact, same-owner session keys.

    Codex stores a thread UUID in transcript metadata while the ledger uses
    the full rollout filename stem as its primary key. Resolution is deferred
    until every live and rotated transcript has been parsed so a child may be
    encountered before its parent. Raw ``parent_thread_id`` remains available
    even when no canonical parent exists; ``parent_session_id`` is NULL unless
    it joins an actual same-user, same-source row and never points to itself.
    """
    conn.execute(
        """
        UPDATE sessions AS child
        SET parent_session_id = (
            SELECT parent.session_id
            FROM sessions AS parent
            WHERE parent.username = child.username
              AND parent.source = child.source
              AND parent.thread_id = child.parent_thread_id
              AND (child.thread_id IS NULL OR parent.thread_id != child.thread_id)
              AND parent.session_id != child.session_id
            ORDER BY COALESCE(parent.first_timestamp, ''), parent.session_id
            LIMIT 1
        )
        WHERE child.parent_thread_id IS NOT NULL
          AND child.parent_thread_id != ''
        """
    )
    # A current Codex row without raw parent evidence is a root. Older raw UUID
    # values in parent_session_id must not survive as dangling pseudo-keys.
    conn.execute(
        """
        UPDATE sessions
        SET parent_session_id = NULL
        WHERE source = 'codex'
          AND (parent_thread_id IS NULL OR parent_thread_id = '')
        """
    )
    # Claude parents are already canonical session ids, but validate them with
    # the same graph-integrity rule and clear unresolved/self references.
    conn.execute(
        """
        UPDATE sessions AS child
        SET parent_session_id = NULL
        WHERE child.parent_session_id IS NOT NULL
          AND (
            child.parent_session_id = child.session_id
            OR NOT EXISTS (
                SELECT 1
                FROM sessions AS parent
                WHERE parent.session_id = child.parent_session_id
                  AND parent.username = child.username
                  AND parent.source = child.source
            )
          )
        """
    )


def _is_rotation_error(exc: OSError) -> bool:
    return exc.errno in {errno.ENOENT, getattr(errno, "ESTALE", errno.ENOENT)}


def _report_rotation_skip(path: Path, exc: OSError, verbose: bool) -> None:
    if verbose:
        print(f"  Skipped rotating session {path}: {exc}", file=sys.stderr)


def _tighten_private_marker(
    conn,
    *,
    existing_row,
    jsonl_path: Path,
    shared_dir: Path,
    marker: PrivateSessionMarker,
    fhash: str,
    file_stat,
    now: str,
) -> None:
    storage_row = dict(existing_row)
    storage_row["source_path"] = str(jsonl_path)
    transition = prepare_private_session_storage(storage_row, shared_dir=shared_dir)
    try:
        if not _harden_managed_artifact(
            transition.archive_path,
            _private_archive_root(shared_dir),
        ) or file_hash(transition.archive_path) != fhash:
            raise OSError(
                errno.EIO,
                "private marker archival copy hash mismatch; source metadata was not advanced",
            )
        conn.execute(
            """
            UPDATE sessions
            SET source_path = ?,
                file_hash = ?,
                file_size = ?,
                file_mtime = ?,
                synced_at = ?
            WHERE session_id = ?
            """,
            (
                str(jsonl_path),
                fhash,
                file_stat.st_size,
                file_stat.st_mtime,
                now,
                existing_row["session_id"],
            ),
        )
        transition_session_visibility(
            conn,
            existing_row["session_id"],
            "private",
            shared_dir=shared_dir,
            transition_source="marker",
            reason=f"inline {marker.marker}",
            manage_storage=False,
            storage_transition=transition,
        )
        _clear_copy_retry(conn, jsonl_path, existing_row["session_id"])
    except OSError as exc:
        if transition.active:
            transition.rollback()
        _record_copy_retry(
            conn,
            source_path=jsonl_path,
            session_id=existing_row["session_id"],
            expected_sha256=fhash,
            file_stat=file_stat,
            error=exc,
        )
        conn.commit()
        raise
    except BaseException:
        if transition.active:
            transition.rollback()
        raise


def sync_sessions(
    shared_dir: Path,
    db_path: Path,
    username: str,
    machine: str,
    home: Path,
    verbose: bool = False,
) -> SyncResult:
    """
    Discover, parse, and copy sessions.
    Returns tuple-compatible counts plus a typed completion status.

    Holds an exclusive lock for the duration: a concurrent sync (e.g. the
    usage-tracker launchd job overlapping a manual run) returns a typed
    lock-contended result instead of interleaving copies onto shared files.
    """
    lock_path = Path(f"{db_path}.sync.lock")
    _secure_mkdir(lock_path.parent, harden_existing=False)
    lock_fd: int | None = None
    try:
        lock_flags = os.O_WRONLY | os.O_CREAT
        lock_flags |= getattr(os, "O_CLOEXEC", 0)
        lock_flags |= getattr(os, "O_NOFOLLOW", 0)
        lock_flags |= getattr(os, "O_NONBLOCK", 0)
        lock_fd = os.open(lock_path, lock_flags, 0o600)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise SyncLockError(
                f"Refusing non-regular sync lock path: {lock_path}"
            )
        os.fchmod(lock_fd, 0o600)
    except SyncLockError:
        if lock_fd is not None:
            os.close(lock_fd)
        raise
    except OSError as exc:
        if lock_fd is not None:
            os.close(lock_fd)
        raise SyncLockError(
            f"Could not safely open sync lock {lock_path}: {exc}"
        ) from exc
    with os.fdopen(lock_fd, "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise SyncLockError(
                    f"Could not acquire sync lock {lock_path}: {exc}"
                ) from exc
            # Always audible: a silent (0, 0, 0) reads as "synced, all quiet"
            # to humans and scripts checking the summary line.
            print("Skipped: another logpile sync holds the lock.", file=sys.stderr)
            return SyncLockContended(0, 0, 0)
        return _sync_sessions(shared_dir, db_path, username, machine, home, verbose)


def _sync_sessions(
    shared_dir: Path,
    db_path: Path,
    username: str,
    machine: str,
    home: Path,
    verbose: bool = False,
) -> SyncResult:
    """Locked body of sync_sessions."""
    init_db(db_path)
    _secure_shared_mkdir(shared_dir, shared_dir)
    patterns = load_ignore_patterns(home)
    now = datetime.now(timezone.utc).isoformat()
    new_count = updated_count = skipped_count = 0

    with get_db(db_path) as conn:
        canonical_username = _resolve_sync_username(conn, username)
        canonical_username = ensure_user(conn, canonical_username, display_name=canonical_username)
        user_row = conn.execute(
            "SELECT default_session_visibility FROM users WHERE username = ?",
            (canonical_username,),
        ).fetchone()
        default_visibility = (
            user_row["default_session_visibility"] if user_row else "unlisted"
        )
        existing = {
            row["session_id"]: row
            for row in conn.execute(
                """
                SELECT
                    session_id,
                    username,
                    file_hash,
                    visibility,
                    visibility_source,
                    tool_call_count,
                    activity_version,
                    narrative_version,
                    session_status,
                    session_summary,
                    objective_family,
                    objective_version,
                    session_origin,
                    origin_version,
                    token_version,
                    identity_version,
                    workspace_root,
                    worktree_root,
                    repo_name,
                    shared_path,
                    source,
                    source_path,
                    project,
                    file_size,
                    file_mtime,
                    EXISTS (
                        SELECT 1 FROM sync_copy_retries scr
                        WHERE scr.source_path = sessions.source_path
                    ) AS copy_retry_pending,
                    (
                        SELECT COUNT(*)
                        FROM session_paths sp
                        WHERE sp.session_id = sessions.session_id
                    ) AS path_count
                FROM sessions
                """
            )
        }
        _planned_bytes, _planned_files, preflight_source_hashes = _preflight_shared_copy_volume(
            home=home,
            shared_dir=shared_dir,
            existing=existing,
            patterns=patterns,
        )
        processed_count = 0
        repo_metadata_cache: dict[str, dict[str, str | int | None]] = {}
        # Sessions whose native_* aggregates must be recomputed before this
        # sync finishes: everything (re)parsed plus previous owners of stolen
        # claims. If an earlier sync died before its refresh (flag not reset
        # to "0"), recompute everything to heal whatever it left behind.
        affected_native: set[str] = set()
        force_full_refresh = get_meta(conn, "native_refresh_pending") != "0"
        set_meta(conn, "native_refresh_pending", "1")
        conn.commit()

        def flush_if_needed() -> None:
            nonlocal processed_count
            processed_count += 1
            if processed_count % 50 == 0:
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        # ── Claude Code sessions ───────────────────────────────────────────────
        claude_root = claude_projects_root(home)
        if claude_root.exists():
            for jsonl_path in sorted(claude_root.rglob("*.jsonl")):
                if should_ignore(jsonl_path, patterns):
                    skipped_count += 1
                    continue

                session_id = jsonl_path.stem
                existing_row = existing.get(session_id)
                if _is_claude_workflow_journal(jsonl_path):
                    # Remove the one legacy stem-keyed progress row when this
                    # exact journal created it. Full agent transcripts remain
                    # indexed under agent-{agentId}; other journals with the
                    # same filename cannot accidentally delete that row.
                    if (
                        existing_row
                        and existing_row["source_path"] == str(jsonl_path)
                    ):
                        claim_keys = [
                            row[0]
                            for row in conn.execute(
                                "SELECT claim_key FROM message_claims "
                                "WHERE session_id = ?",
                                (session_id,),
                            )
                        ]
                        for chunk in (
                            claim_keys[start:start + 500]
                            for start in range(0, len(claim_keys), 500)
                        ):
                            placeholders = ",".join("?" * len(chunk))
                            affected_native.update(
                                row[0]
                                for row in conn.execute(
                                    f"SELECT DISTINCT session_id FROM message_claims "
                                    f"WHERE claim_key IN ({placeholders})",
                                    chunk,
                                )
                            )
                        for table in (
                            "message_claims",
                            "tool_calls",
                            "session_paths",
                            "session_daily_usage",
                        ):
                            conn.execute(
                                f"DELETE FROM {table} WHERE session_id = ?",
                                (session_id,),
                            )
                        conn.execute(
                            "DELETE FROM sessions WHERE session_id = ?",
                            (session_id,),
                        )
                        updated_count += 1
                        flush_if_needed()
                    else:
                        skipped_count += 1
                    continue
                needs_structure_backfill = bool(
                    existing_row and (
                        (existing_row["workspace_root"] or "") == ""
                        or (existing_row["worktree_root"] or "") == ""
                        or (existing_row["repo_name"] or "") == ""
                        or (existing_row["activity_version"] or 0) < SESSION_ACTIVITY_VERSION
                        or (existing_row["narrative_version"] or 0) < SESSION_NARRATIVE_VERSION
                        or (existing_row["session_status"] or "") == ""
                        or (existing_row["session_summary"] or "") == ""
                        or (existing_row["objective_version"] or 0) < SESSION_OBJECTIVE_VERSION
                        or (existing_row["origin_version"] or 0) < SESSION_ORIGIN_VERSION
                        or (existing_row["token_version"] or 0) < SESSION_TOKEN_VERSION
                        or (existing_row["identity_version"] or 0) < SESSION_IDENTITY_VERSION
                        or (existing_row["session_origin"] or "") == ""
                        or (
                            (existing_row["tool_call_count"] or 0) > 0
                            and not existing_row["path_count"]
                        )
                    )
                )

                if (
                    existing_row
                    and not needs_structure_backfill
                    and _unchanged_on_disk(
                        existing_row,
                        jsonl_path,
                        shared_dir,
                        preflight_source=preflight_source_hashes.get(str(jsonl_path)),
                    )
                ):
                    skipped_count += 1
                    continue

                try:
                    # stat BEFORE hashing: a write landing mid-hash then makes
                    # the stored size/mtime stale, forcing a re-parse next
                    # sync instead of silently skipping the newer content.
                    file_stat = jsonl_path.stat()
                    fhash = file_hash(jsonl_path)
                except OSError as exc:
                    if exc.errno == errno.ENOSPC:
                        raise
                    _report_rotation_skip(jsonl_path, exc, verbose)
                    skipped_count += 1
                    continue

                if existing_row and existing_row["file_hash"] == fhash and not needs_structure_backfill:
                    try:
                        shared_path, storage_changed = _sync_shared_copy(
                            conn=conn,
                            src=jsonl_path,
                            shared_dir=shared_dir,
                            username=canonical_username,
                            source=existing_row["source"],
                            project=existing_row["project"] or project_from_claude_path(jsonl_path),
                            filename=jsonl_path.name,
                            visibility=existing_row["visibility"],
                            expected_sha256=fhash,
                            existing_shared_path=existing_row["shared_path"],
                        )
                    except OSError as exc:
                        _record_copy_retry(
                            conn,
                            source_path=jsonl_path,
                            session_id=session_id,
                            expected_sha256=fhash,
                            file_stat=file_stat,
                            error=exc,
                        )
                        conn.commit()
                        _report_rotation_skip(jsonl_path, exc, verbose)
                        skipped_count += 1
                        continue
                    _clear_copy_retry(conn, jsonl_path, session_id)
                    row_moved = (
                        existing_row["source_path"] != str(jsonl_path)
                        or existing_row["file_size"] != file_stat.st_size
                        or existing_row["file_mtime"] is None
                        or abs(existing_row["file_mtime"] - file_stat.st_mtime) > 1e-6
                    )
                    if storage_changed or row_moved or shared_path != (existing_row["shared_path"] or ""):
                        conn.execute(
                            """
                            UPDATE sessions
                            SET shared_path = ?, source_path = ?, file_size = ?,
                                file_mtime = ?, synced_at = ?
                            WHERE session_id = ?
                            """,
                            (shared_path, str(jsonl_path), file_stat.st_size,
                             file_stat.st_mtime, now, session_id),
                        )
                        flush_if_needed()
                        updated_count += 1
                    else:
                        skipped_count += 1
                    continue

                try:
                    info = parse_claudecode_session(jsonl_path)
                except OSError as exc:
                    if exc.errno == errno.ENOSPC:
                        raise
                    _report_rotation_skip(jsonl_path, exc, verbose)
                    skipped_count += 1
                    continue
                if info is None:
                    skipped_count += 1
                    continue
                if isinstance(info, PrivateSessionMarker):
                    if existing_row:
                        try:
                            _tighten_private_marker(
                                conn,
                                existing_row=existing_row,
                                jsonl_path=jsonl_path,
                                shared_dir=shared_dir,
                                marker=info,
                                fhash=fhash,
                                file_stat=file_stat,
                                now=now,
                            )
                        except OSError as exc:
                            if exc.errno == errno.ENOSPC:
                                raise
                            _report_rotation_skip(jsonl_path, exc, verbose)
                            skipped_count += 1
                            continue
                        flush_if_needed()
                        updated_count += 1
                    else:
                        skipped_count += 1
                    continue

                project = project_from_claude_path(jsonl_path)
                workspace_root = _normalize_workspace_root(
                    info.workspace_root or (info.project if info.project != "unknown" else None)
                )
                repo_metadata = _resolve_repo_metadata(workspace_root, repo_metadata_cache)
                session_paths = _annotate_session_paths(
                    info.session_paths,
                    repo_root=repo_metadata["repo_root"],
                    worktree_root=repo_metadata["worktree_root"],
                    workspace_root=workspace_root,
                )
                activity = _derive_session_activity(info.tool_calls, session_paths)
                narrative = _derive_session_narrative(
                    source="claudecode",
                    project=project,
                    repo_name=repo_metadata["repo_name"],
                    first_user_message=info.first_user_message,
                    error_count=info.error_count,
                    activity=activity,
                    session_paths=session_paths,
                )
                origin = derive_session_origin(
                    source="claudecode",
                    session_id=info.session_id,
                    first_user_message=info.first_user_message,
                    project=project,
                    workspace_root=workspace_root,
                    source_path=str(jsonl_path),
                )
                objective = derive_session_objective(
                    narrative["session_goal"],
                    info.first_user_message,
                    narrative["session_summary"],
                )
                visibility = resolve_session_visibility(
                    conn,
                    username=canonical_username,
                    source="claudecode",
                    default_visibility=default_visibility,
                    session_data={
                        "project": project,
                        "source_path": str(jsonl_path),
                        "first_user_message": info.first_user_message,
                        "model": info.model,
                        "machine": machine,
                        "username": canonical_username,
                    },
                )
                storage_visibility = _effective_storage_visibility(existing_row, visibility)
                try:
                    shared_path, _ = _sync_shared_copy(
                        conn=conn,
                        src=jsonl_path,
                        shared_dir=shared_dir,
                        username=canonical_username,
                        source="claudecode",
                        project=project,
                        filename=jsonl_path.name,
                        visibility=storage_visibility,
                        expected_sha256=fhash,
                        existing_shared_path=existing_row["shared_path"] if existing_row else None,
                    )
                except OSError as exc:
                    _record_copy_retry(
                        conn,
                        source_path=jsonl_path,
                        session_id=info.session_id,
                        expected_sha256=fhash,
                        file_stat=file_stat,
                        error=exc,
                    )
                    conn.commit()
                    _report_rotation_skip(jsonl_path, exc, verbose)
                    skipped_count += 1
                    continue
                _clear_copy_retry(conn, jsonl_path, info.session_id)

                upsert_session(conn, {
                    "session_id": info.session_id,
                    "source": "claudecode",
                    "username": canonical_username,
                    "machine": machine,
                    "project": project,
                    "workspace_root": workspace_root,
                    "worktree_root": repo_metadata["worktree_root"],
                    "repo_root": repo_metadata["repo_root"],
                    "repo_name": repo_metadata["repo_name"],
                    "git_branch": repo_metadata["git_branch"],
                    "git_commit": repo_metadata["git_commit"],
                    "git_dirty": repo_metadata["git_dirty"],
                    "source_path": str(jsonl_path),
                    "shared_path": shared_path,
                    "first_timestamp": info.first_timestamp,
                    "last_timestamp": info.last_timestamp,
                    "duration_seconds": _compute_duration(info.first_timestamp, info.last_timestamp),
                    "user_message_count": info.user_message_count,
                    "assistant_message_count": info.assistant_message_count,
                    "tool_call_count": info.tool_call_count,
                    "error_count": info.error_count,
                    "total_input_tokens": info.total_input_tokens,
                    "total_output_tokens": info.total_output_tokens,
                    "fresh_input_tokens": info.fresh_input_tokens,
                    "cached_input_tokens": info.cached_input_tokens,
                    "cache_creation_input_tokens": info.cache_creation_input_tokens,
                    "cache_creation_5m_input_tokens": info.cache_creation_5m_input_tokens,
                    "cache_creation_1h_input_tokens": info.cache_creation_1h_input_tokens,
                    "cache_creation_unknown_input_tokens": info.cache_creation_unknown_input_tokens,
                    "reasoning_output_tokens": info.reasoning_output_tokens,
                    "token_version": SESSION_TOKEN_VERSION,
                    "identity_version": SESSION_IDENTITY_VERSION,
                    "first_user_message": info.first_user_message,
                    "parent_session_id": info.parent_session_id,
                    "thread_id": info.thread_id,
                    "parent_thread_id": info.parent_thread_id,
                    "spawn_depth": info.spawn_depth,
                    "visibility": visibility["visibility"],
                    "visibility_source": visibility["visibility_source"],
                    "visibility_rule_id": visibility["visibility_rule_id"],
                    "visibility_reason": visibility["visibility_reason"],
                    "is_private": 1 if visibility["visibility"] == "private" else 0,
                    "file_hash": fhash,
                    "file_size": file_stat.st_size,
                    "file_mtime": file_stat.st_mtime,
                    "synced_at": now,
                    "model": info.model,
                    **activity,
                    **narrative,
                    **objective,
                    **origin,
                })
                insert_tool_calls(conn, info.session_id, info.tool_calls)
                insert_session_paths(conn, info.session_id, session_paths)
                insert_session_daily_usage(conn, info.session_id, info.daily_usage)
                affected_native.add(info.session_id)
                affected_native |= apply_message_claims(
                    conn, info.session_id, info.message_usage
                )
                flush_if_needed()

                action = "Updated" if session_id in existing else "Added"
                if existing_row:
                    updated_count += 1
                else:
                    new_count += 1

                if verbose:
                    print(f"  {action}: {session_id[:12]}… ({project})")

        # ── Codex sessions ─────────────────────────────────────────────────────
        seen_codex_stems: set[str] = set()
        for codex_root in codex_session_roots(home):
            for jsonl_path in sorted(codex_root.rglob("*.jsonl")):
                if should_ignore(jsonl_path, patterns):
                    skipped_count += 1
                    continue

                session_id = jsonl_path.stem  # full filename stem as unique ID
                if session_id in seen_codex_stems:
                    skipped_count += 1
                    continue
                seen_codex_stems.add(session_id)
                existing_row = existing.get(session_id)
                needs_structure_backfill = bool(
                    existing_row and (
                        (existing_row["workspace_root"] or "") == ""
                        or (existing_row["worktree_root"] or "") == ""
                        or (existing_row["repo_name"] or "") == ""
                        or (existing_row["activity_version"] or 0) < SESSION_ACTIVITY_VERSION
                        or (existing_row["narrative_version"] or 0) < SESSION_NARRATIVE_VERSION
                        or (existing_row["session_status"] or "") == ""
                        or (existing_row["session_summary"] or "") == ""
                        or (existing_row["objective_version"] or 0) < SESSION_OBJECTIVE_VERSION
                        or (existing_row["origin_version"] or 0) < SESSION_ORIGIN_VERSION
                        or (existing_row["token_version"] or 0) < SESSION_TOKEN_VERSION
                        or (existing_row["identity_version"] or 0) < SESSION_IDENTITY_VERSION
                        or (existing_row["session_origin"] or "") == ""
                        or (
                            (existing_row["tool_call_count"] or 0) > 0
                            and not existing_row["path_count"]
                        )
                    )
                )

                if (
                    existing_row
                    and not needs_structure_backfill
                    and _unchanged_on_disk(
                        existing_row,
                        jsonl_path,
                        shared_dir,
                        preflight_source=preflight_source_hashes.get(str(jsonl_path)),
                    )
                ):
                    skipped_count += 1
                    continue

                try:
                    # stat BEFORE hashing — see the Claude loop.
                    file_stat = jsonl_path.stat()
                    fhash = file_hash(jsonl_path)
                except OSError as exc:
                    if exc.errno == errno.ENOSPC:
                        raise
                    _report_rotation_skip(jsonl_path, exc, verbose)
                    seen_codex_stems.discard(session_id)
                    skipped_count += 1
                    continue

                if existing_row and existing_row["file_hash"] == fhash and not needs_structure_backfill:
                    try:
                        shared_path, storage_changed = _sync_shared_copy(
                            conn=conn,
                            src=jsonl_path,
                            shared_dir=shared_dir,
                            username=canonical_username,
                            source=existing_row["source"],
                            project=existing_row["project"] or "unknown",
                            filename=jsonl_path.name,
                            visibility=existing_row["visibility"],
                            expected_sha256=fhash,
                            existing_shared_path=existing_row["shared_path"],
                        )
                    except OSError as exc:
                        _record_copy_retry(
                            conn,
                            source_path=jsonl_path,
                            session_id=session_id,
                            expected_sha256=fhash,
                            file_stat=file_stat,
                            error=exc,
                        )
                        conn.commit()
                        _report_rotation_skip(jsonl_path, exc, verbose)
                        seen_codex_stems.discard(session_id)
                        skipped_count += 1
                        continue
                    _clear_copy_retry(conn, jsonl_path, session_id)
                    row_moved = (
                        existing_row["source_path"] != str(jsonl_path)
                        or existing_row["file_size"] != file_stat.st_size
                        or existing_row["file_mtime"] is None
                        or abs(existing_row["file_mtime"] - file_stat.st_mtime) > 1e-6
                    )
                    if storage_changed or row_moved or shared_path != (existing_row["shared_path"] or ""):
                        conn.execute(
                            """
                            UPDATE sessions
                            SET shared_path = ?, source_path = ?, file_size = ?,
                                file_mtime = ?, synced_at = ?
                            WHERE session_id = ?
                            """,
                            (shared_path, str(jsonl_path), file_stat.st_size,
                             file_stat.st_mtime, now, session_id),
                        )
                        flush_if_needed()
                        updated_count += 1
                    else:
                        skipped_count += 1
                    continue

                try:
                    info = parse_codex_session(jsonl_path)
                except OSError as exc:
                    if exc.errno == errno.ENOSPC:
                        raise
                    _report_rotation_skip(jsonl_path, exc, verbose)
                    seen_codex_stems.discard(session_id)
                    skipped_count += 1
                    continue
                if info is None:
                    # _load_jsonl deliberately contains read errors and returns
                    # no records. If Codex archived the rollout between hashing
                    # and parsing, let the later archived-root pass retry the
                    # same stem instead of treating the vanished live path as
                    # the winning copy for this run.
                    if not jsonl_path.exists():
                        seen_codex_stems.discard(session_id)
                    skipped_count += 1
                    continue
                if isinstance(info, PrivateSessionMarker):
                    if existing_row:
                        try:
                            _tighten_private_marker(
                                conn,
                                existing_row=existing_row,
                                jsonl_path=jsonl_path,
                                shared_dir=shared_dir,
                                marker=info,
                                fhash=fhash,
                                file_stat=file_stat,
                                now=now,
                            )
                        except OSError as exc:
                            if exc.errno == errno.ENOSPC:
                                raise
                            _report_rotation_skip(jsonl_path, exc, verbose)
                            seen_codex_stems.discard(session_id)
                            skipped_count += 1
                            continue
                        flush_if_needed()
                        updated_count += 1
                    else:
                        skipped_count += 1
                    continue

                # Use leaf of cwd as project name
                project = Path(info.project).name if info.project != "unknown" else "unknown"
                workspace_root = _normalize_workspace_root(
                    info.workspace_root or (info.project if info.project != "unknown" else None)
                )
                repo_metadata = _resolve_repo_metadata(workspace_root, repo_metadata_cache)
                session_paths = _annotate_session_paths(
                    info.session_paths,
                    repo_root=repo_metadata["repo_root"],
                    worktree_root=repo_metadata["worktree_root"],
                    workspace_root=workspace_root,
                )
                activity = _derive_session_activity(info.tool_calls, session_paths)
                narrative = _derive_session_narrative(
                    source="codex",
                    project=project,
                    repo_name=repo_metadata["repo_name"],
                    first_user_message=info.first_user_message,
                    error_count=info.error_count,
                    activity=activity,
                    session_paths=session_paths,
                )
                origin = derive_session_origin(
                    source="codex",
                    session_id=session_id,
                    first_user_message=info.first_user_message,
                    project=project,
                    workspace_root=workspace_root,
                    source_path=str(jsonl_path),
                )
                objective = derive_session_objective(
                    narrative["session_goal"],
                    info.first_user_message,
                    narrative["session_summary"],
                )
                visibility = resolve_session_visibility(
                    conn,
                    username=canonical_username,
                    source="codex",
                    default_visibility=default_visibility,
                    session_data={
                        "project": project,
                        "source_path": str(jsonl_path),
                        "first_user_message": info.first_user_message,
                        "model": info.model,
                        "machine": machine,
                        "username": canonical_username,
                    },
                )
                storage_visibility = _effective_storage_visibility(existing_row, visibility)
                try:
                    shared_path, _ = _sync_shared_copy(
                        conn=conn,
                        src=jsonl_path,
                        shared_dir=shared_dir,
                        username=canonical_username,
                        source="codex",
                        project=project,
                        filename=jsonl_path.name,
                        visibility=storage_visibility,
                        expected_sha256=fhash,
                        existing_shared_path=existing_row["shared_path"] if existing_row else None,
                    )
                except OSError as exc:
                    _record_copy_retry(
                        conn,
                        source_path=jsonl_path,
                        session_id=session_id,
                        expected_sha256=fhash,
                        file_stat=file_stat,
                        error=exc,
                    )
                    conn.commit()
                    _report_rotation_skip(jsonl_path, exc, verbose)
                    seen_codex_stems.discard(session_id)
                    skipped_count += 1
                    continue
                _clear_copy_retry(conn, jsonl_path, session_id)

                # Use file_stem as session_id (unique per file)
                upsert_session(conn, {
                    "session_id": session_id,
                    "source": "codex",
                    "username": canonical_username,
                    "machine": machine,
                    "project": project,
                    "workspace_root": workspace_root,
                    "worktree_root": repo_metadata["worktree_root"],
                    "repo_root": repo_metadata["repo_root"],
                    "repo_name": repo_metadata["repo_name"],
                    "git_branch": repo_metadata["git_branch"],
                    "git_commit": repo_metadata["git_commit"],
                    "git_dirty": repo_metadata["git_dirty"],
                    "source_path": str(jsonl_path),
                    "shared_path": shared_path,
                    "first_timestamp": info.first_timestamp,
                    "last_timestamp": info.last_timestamp,
                    "duration_seconds": None,
                    "user_message_count": info.user_message_count,
                    "assistant_message_count": info.assistant_message_count,
                    "tool_call_count": info.tool_call_count,
                    "error_count": info.error_count,
                    "total_input_tokens": info.total_input_tokens,
                    "total_output_tokens": info.total_output_tokens,
                    "fresh_input_tokens": info.fresh_input_tokens,
                    "cached_input_tokens": info.cached_input_tokens,
                    "cache_creation_input_tokens": info.cache_creation_input_tokens,
                    "cache_creation_5m_input_tokens": info.cache_creation_5m_input_tokens,
                    "cache_creation_1h_input_tokens": info.cache_creation_1h_input_tokens,
                    "cache_creation_unknown_input_tokens": info.cache_creation_unknown_input_tokens,
                    "reasoning_output_tokens": info.reasoning_output_tokens,
                    "token_version": SESSION_TOKEN_VERSION,
                    "identity_version": SESSION_IDENTITY_VERSION,
                    "first_user_message": info.first_user_message,
                    # Codex metadata carries a raw thread UUID here; the exact
                    # rollout-key parent is resolved after the complete scan.
                    "parent_session_id": None,
                    "thread_id": info.thread_id,
                    "parent_thread_id": info.parent_thread_id,
                    "spawn_depth": info.spawn_depth,
                    "visibility": visibility["visibility"],
                    "visibility_source": visibility["visibility_source"],
                    "visibility_rule_id": visibility["visibility_rule_id"],
                    "visibility_reason": visibility["visibility_reason"],
                    "is_private": 1 if visibility["visibility"] == "private" else 0,
                    "file_hash": fhash,
                    "file_size": file_stat.st_size,
                    "file_mtime": file_stat.st_mtime,
                    "synced_at": now,
                    "model": info.model,
                    **activity,
                    **narrative,
                    **objective,
                    **origin,
                })
                insert_tool_calls(conn, session_id, info.tool_calls)
                insert_session_paths(conn, session_id, session_paths)
                insert_session_daily_usage(conn, session_id, info.daily_usage)
                affected_native.add(session_id)
                flush_if_needed()

                action = "Updated" if session_id in existing else "Added"
                if existing_row:
                    updated_count += 1
                else:
                    new_count += 1

                if verbose:
                    short = session_id[-20:] if len(session_id) > 20 else session_id
                    print(f"  {action}: …{short} ({project})")

        backfilled, backfill_affected = _backfill_tokens_from_shared(conn, verbose=verbose)
        updated_count += backfilled
        affected_native |= backfill_affected

        _resolve_canonical_parents(conn)
        refresh_native_usage(conn, None if force_full_refresh else affected_native)
        set_meta(conn, "native_refresh_pending", "0")

    return SyncResult(new_count, updated_count, skipped_count)
