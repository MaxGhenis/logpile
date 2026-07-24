"""Cloud backup and exact raw-text indexing for Logpile."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .discovery import (
    DiscoveredTranscript,
    discover_transcripts,
    transcript_roots,
)
from .parsers import _extract_text, _normalize_codex_record

MAX_TEXT_CHARS = 12_000
CHUNK_OVERLAP_CHARS = 400

SUPABASE_BASE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS logpile_raw_objects (
    sha256 TEXT PRIMARY KEY,
    size_bytes BIGINT NOT NULL,
    object_provider TEXT NOT NULL,
    bucket TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL,
    uploaded_at TIMESTAMPTZ,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS logpile_raw_files (
    file_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL REFERENCES logpile_raw_objects(sha256) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    mtime_ns BIGINT NOT NULL,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_at TIMESTAMPTZ,
    UNIQUE (source_path, sha256)
);

CREATE TABLE IF NOT EXISTS logpile_raw_chunks (
    file_id TEXT NOT NULL REFERENCES logpile_raw_files(file_id) ON DELETE CASCADE,
    event_index INTEGER NOT NULL,
    fragment_index INTEGER NOT NULL DEFAULT 0,
    chunk_index INTEGER NOT NULL,
    session_id TEXT,
    source TEXT NOT NULL,
    record_type TEXT,
    role TEXT,
    tool_name TEXT,
    timestamp_text TEXT,
    byte_start BIGINT,
    byte_end BIGINT,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (file_id, event_index, fragment_index, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_logpile_raw_files_sha256
    ON logpile_raw_files(sha256);
CREATE INDEX IF NOT EXISTS idx_logpile_raw_files_source
    ON logpile_raw_files(source);
CREATE INDEX IF NOT EXISTS idx_logpile_raw_files_relative_path
    ON logpile_raw_files(relative_path);
CREATE INDEX IF NOT EXISTS idx_logpile_raw_chunks_session
    ON logpile_raw_chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_logpile_raw_chunks_source
    ON logpile_raw_chunks(source);
CREATE INDEX IF NOT EXISTS idx_logpile_raw_chunks_role
    ON logpile_raw_chunks(role);
"""

SUPABASE_SEARCH_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_logpile_raw_chunks_fts
    ON logpile_raw_chunks USING gin (to_tsvector('english', content))
    WHERE role IS DISTINCT FROM 'tool_result';
"""

SUPABASE_SCHEMA_SQL = f"{SUPABASE_BASE_SCHEMA_SQL}\n{SUPABASE_SEARCH_INDEX_SQL}"


@dataclass(frozen=True)
class RawFileCandidate:
    path: Path
    relative_path: str
    source: str
    size_bytes: int
    mtime_ns: int
    sha256: str
    file_id: str
    object_key: str
    content_type: str
    upload_path: Path | None = None

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def payload_path(self) -> Path:
        return self.upload_path or self.path


@dataclass(frozen=True)
class BackupPlan:
    candidates: list[RawFileCandidate]

    @property
    def total_bytes(self) -> int:
        return sum(candidate.size_bytes for candidate in self.candidates)

    @property
    def source_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for candidate in self.candidates:
            counts[candidate.source] = counts.get(candidate.source, 0) + 1
        return counts

    @property
    def source_bytes(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for candidate in self.candidates:
            totals[candidate.source] = (
                totals.get(candidate.source, 0) + candidate.size_bytes
            )
        return totals


@dataclass(frozen=True)
class TextFragment:
    record_type: str | None
    role: str | None
    tool_name: str | None
    content: str


@dataclass(frozen=True)
class RawTextChunk:
    file_id: str
    event_index: int
    fragment_index: int
    chunk_index: int
    session_id: str | None
    source: str
    record_type: str | None
    role: str | None
    tool_name: str | None
    timestamp_text: str | None
    byte_start: int
    byte_end: int
    content: str
    content_sha256: str


@dataclass(frozen=True)
class R2Config:
    bucket: str
    endpoint_url: str
    access_key_id: str | None = None
    secret_access_key: str | None = None
    provider: str = "r2"


def format_bytes(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_readonly_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


@contextmanager
def _private_umask() -> Iterator[None]:
    """Keep SQLite-created staging files private from their first write."""
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def verify_sqlite_snapshot(path: Path) -> None:
    """Raise when *path* is not a self-consistent SQLite database."""
    try:
        with sqlite3.connect(_sqlite_readonly_uri(path), uri=True) as conn:
            results = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(
            f"SQLite snapshot verification failed for {path}: {exc}"
        ) from exc
    if results != ["ok"]:
        detail = "; ".join(results) if results else "quick_check returned no result"
        raise RuntimeError(f"SQLite snapshot verification failed for {path}: {detail}")


def create_sqlite_snapshot(source: Path, destination: Path) -> Path:
    """Create and verify a point-in-time SQLite copy with ``VACUUM INTO``.

    SQLite reads the source and its active WAL as one transaction. The
    verified temporary database is then atomically installed at the lexical
    destination, so an interrupted backup never leaves a partial output.
    """
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise RuntimeError(f"SQLite database not found: {source}")
    if source.resolve() == destination.resolve():
        raise RuntimeError(
            "SQLite backup destination must differ from the source database."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)
    # VACUUM INTO refuses to write to an existing file. mkstemp reserves a
    # collision-free lexical path first; unlink only that empty reservation.
    temp_path.unlink()
    try:
        try:
            with (
                _private_umask(),
                sqlite3.connect(
                    _sqlite_readonly_uri(source),
                    uri=True,
                    timeout=30,
                ) as conn,
            ):
                conn.execute("VACUUM INTO ?", (str(temp_path),))
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Could not snapshot SQLite database {source}: {exc}"
            ) from exc

        os.chmod(temp_path, 0o600)
        verify_sqlite_snapshot(temp_path)
        os.replace(temp_path, destination)
        os.chmod(destination, 0o600)
        return destination
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def copy_prefix_with_sha256(source: Path, destination: Path, size_bytes: int) -> str:
    digest = hashlib.sha256()
    remaining = size_bytes
    with source.open("rb") as src, destination.open("wb") as dst:
        while remaining > 0:
            chunk = src.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            dst.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except (OSError, ValueError):
        return str(path)


def _source_for_path(path: Path, home: Path) -> str | None:
    absolute = Path(os.path.abspath(path))
    for root in transcript_roots(home):
        try:
            absolute.relative_to(Path(os.path.abspath(root.path)))
            return root.source
        except ValueError:
            continue
    rel = _safe_relative(path, home)
    if rel.startswith(".codex/logs_2.sqlite"):
        return "codex_db"
    return None


def _content_type(path: Path) -> str:
    suffix = _raw_suffix(path)
    if suffix == ".jsonl":
        return "application/x-ndjson"
    if suffix == ".sqlite":
        return "application/vnd.sqlite3"
    if suffix in {".wal", ".shm", ".sqlite-wal", ".sqlite-shm"}:
        return "application/octet-stream"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _raw_suffix(path: Path) -> str:
    name = path.name.lower()
    for suffix in (".sqlite-wal", ".sqlite-shm"):
        if name.endswith(suffix):
            return suffix
    return path.suffix.lower()


def object_key_for(sha256: str, path: Path) -> str:
    suffix = _raw_suffix(path)
    if suffix not in {
        ".jsonl",
        ".json",
        ".sqlite",
        ".wal",
        ".shm",
        ".sqlite-wal",
        ".sqlite-shm",
    }:
        suffix = ".bin"
    return f"raw/sha256/{sha256[:2]}/{sha256}{suffix}"


def file_id_for(relative_path: str, sha256: str) -> str:
    return hashlib.sha256(f"{relative_path}\0{sha256}".encode()).hexdigest()


def build_candidate(
    path: Path,
    *,
    home: Path,
    source: str | None = None,
) -> RawFileCandidate:
    stat = path.stat()
    relative_path = _safe_relative(path, home)
    sha256 = sha256_file(path)
    source = source or _source_for_path(path, home) or "other"
    return RawFileCandidate(
        path=path,
        relative_path=relative_path,
        source=source,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=sha256,
        file_id=file_id_for(relative_path, sha256),
        object_key=object_key_for(sha256, path),
        content_type=_content_type(path),
    )


@contextmanager
def snapshot_candidate(
    path: Path,
    *,
    home: Path,
    source: str | None = None,
    temp_dir: Path | None = None,
) -> Iterator[RawFileCandidate]:
    stat = path.stat()
    suffix = _raw_suffix(path)
    fd, temp_name = tempfile.mkstemp(
        prefix="logpile-raw-",
        suffix=suffix if suffix else ".bin",
        dir=str(temp_dir) if temp_dir else None,
    )
    os.close(fd)
    snapshot_path = Path(temp_name)
    try:
        if suffix == ".sqlite":
            snapshot_path.unlink()
            create_sqlite_snapshot(path, snapshot_path)
            sha256 = sha256_file(snapshot_path)
        else:
            sha256 = copy_prefix_with_sha256(path, snapshot_path, stat.st_size)
            os.chmod(snapshot_path, 0o600)
        snapshot_size = snapshot_path.stat().st_size
        relative_path = _safe_relative(path, home)
        source = source or _source_for_path(path, home) or "other"
        yield RawFileCandidate(
            path=path,
            relative_path=relative_path,
            source=source,
            size_bytes=snapshot_size,
            mtime_ns=stat.st_mtime_ns,
            sha256=sha256,
            file_id=file_id_for(relative_path, sha256),
            object_key=object_key_for(sha256, path),
            content_type=_content_type(path),
            upload_path=snapshot_path,
        )
    finally:
        try:
            snapshot_path.unlink()
        except FileNotFoundError:
            pass


def _discover_raw_files(
    home: Path,
    *,
    db_path: Path | None = None,
    shared_dir: Path | None = None,
    include_codex_db: bool = True,
) -> Iterator[DiscoveredTranscript]:
    yield from discover_transcripts(
        home,
        db_path=db_path,
        shared_dir=shared_dir,
    )
    if include_codex_db:
        path = home / ".codex" / "logs_2.sqlite"
        if path.is_file():
            # WAL and SHM are deliberately excluded. snapshot_candidate()
            # folds committed WAL pages into one verified SQLite database.
            yield DiscoveredTranscript(path, "codex_db")


def discover_raw_paths(
    home: Path,
    *,
    db_path: Path | None = None,
    shared_dir: Path | None = None,
    include_codex_db: bool = True,
) -> Iterator[Path]:
    """Yield every raw path backup considers, before content deduplication."""

    for discovered in _discover_raw_files(
        home,
        db_path=db_path,
        shared_dir=shared_dir,
        include_codex_db=include_codex_db,
    ):
        yield discovered.path


def plan_backup(
    *,
    home: Path,
    db_path: Path | None = None,
    shared_dir: Path | None = None,
    include_codex_db: bool = True,
    limit: int | None = None,
) -> BackupPlan:
    candidates: list[RawFileCandidate] = []
    seen_sha256s: set[str] = set()
    for discovered in _discover_raw_files(
        home,
        db_path=db_path,
        shared_dir=shared_dir,
        include_codex_db=include_codex_db,
    ):
        path = discovered.path
        if _raw_suffix(path) == ".sqlite":
            with snapshot_candidate(
                path,
                home=home,
                source=discovered.source,
            ) as candidate:
                candidate = replace(candidate, upload_path=None)
        else:
            candidate = build_candidate(
                path,
                home=home,
                source=discovered.source,
            )
        if candidate.sha256 in seen_sha256s:
            continue
        seen_sha256s.add(candidate.sha256)
        candidates.append(candidate)
        if limit is not None and len(candidates) >= limit:
            break
    return BackupPlan(candidates=candidates)


def candidate_to_dict(candidate: RawFileCandidate) -> dict:
    return {
        "path": str(candidate.path),
        "relative_path": candidate.relative_path,
        "source": candidate.source,
        "size_bytes": candidate.size_bytes,
        "mtime_ns": candidate.mtime_ns,
        "sha256": candidate.sha256,
        "file_id": candidate.file_id,
        "object_key": candidate.object_key,
        "content_type": candidate.content_type,
    }


def plan_to_dict(plan: BackupPlan) -> dict:
    return {
        "file_count": len(plan.candidates),
        "total_bytes": plan.total_bytes,
        "total_human": format_bytes(plan.total_bytes),
        "source_counts": plan.source_counts,
        "source_bytes": plan.source_bytes,
        "files": [candidate_to_dict(candidate) for candidate in plan.candidates],
    }


def _sql_statements(sql: str) -> Iterator[str]:
    buffer: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buffer).strip()
            if statement:
                yield statement
            buffer.clear()
    if buffer:
        yield "\n".join(buffer).strip()


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_text(value: str | None) -> str:
    return (value or "").replace("\x00", "").strip()


def _fragment_from_content(
    content,
    *,
    record_type: str | None,
    role: str | None,
    tool_name: str | None = None,
) -> list[TextFragment]:
    if content is None:
        return []

    if isinstance(content, str):
        text = _normalize_text(content)
        return [TextFragment(record_type, role, tool_name, text)] if text else []

    if isinstance(content, list):
        fragments: list[TextFragment] = []
        for block in content:
            fragments.extend(
                _fragment_from_content(
                    block,
                    record_type=record_type,
                    role=role,
                    tool_name=tool_name,
                )
            )
        return fragments

    if not isinstance(content, dict):
        text = _normalize_text(str(content))
        return [TextFragment(record_type, role, tool_name, text)] if text else []

    block_type = content.get("type") or record_type
    if block_type in {"tool_use", "function_call"}:
        name = content.get("name") or tool_name
        payload = content.get("input", content.get("arguments", content))
        text = _normalize_text(_json_dumps(payload))
        return [TextFragment(record_type, "tool_use", name, text)] if text else []

    if block_type in {"tool_result", "function_call_output"}:
        payload = content.get("content", content.get("output", content))
        if isinstance(payload, str):
            text = _normalize_text(payload)
        elif isinstance(payload, list):
            text = _normalize_text(_extract_text(payload) or _json_dumps(payload))
        else:
            text = _normalize_text(_json_dumps(payload))
        return (
            [TextFragment(record_type, "tool_result", tool_name, text)] if text else []
        )

    if "text" in content:
        text = _normalize_text(str(content.get("text") or ""))
    elif "thinking" in content:
        text = _normalize_text(str(content.get("thinking") or ""))
    elif "content" in content:
        return _fragment_from_content(
            content.get("content"),
            record_type=record_type,
            role=role,
            tool_name=tool_name,
        )
    else:
        text = _normalize_text(_extract_text(content))

    return [TextFragment(record_type, role, tool_name, text)] if text else []


def _record_session_id(
    record: dict, record_type: str | None, payload: dict | None
) -> str | None:
    for container in (payload, record):
        if not isinstance(container, dict):
            continue
        for key in ("session_id", "sessionId", "conversation_id"):
            value = container.get(key)
            if value:
                return str(value)
    if (
        record_type == "session_meta"
        and isinstance(payload, dict)
        and payload.get("id")
    ):
        return str(payload["id"])
    return None


def _codex_fragments(record: dict) -> tuple[list[TextFragment], str | None, str | None]:
    record_type, payload, timestamp = _normalize_codex_record(record)
    session_id = _record_session_id(record, record_type, payload)

    if record_type == "message":
        role = payload.get("role") if isinstance(payload, dict) else None
        return (
            _fragment_from_content(
                payload.get("content") if isinstance(payload, dict) else None,
                record_type=record_type,
                role=role,
            ),
            timestamp,
            session_id,
        )

    if record_type == "function_call" and isinstance(payload, dict):
        arguments = payload.get("arguments")
        text = arguments if isinstance(arguments, str) else _json_dumps(arguments or {})
        fragment = TextFragment(
            record_type, "tool_use", payload.get("name"), _normalize_text(text)
        )
        return ([fragment] if fragment.content else []), timestamp, session_id

    if record_type == "function_call_output" and isinstance(payload, dict):
        output = payload.get("output", "")
        text = output if isinstance(output, str) else _json_dumps(output)
        fragment = TextFragment(record_type, "tool_result", None, _normalize_text(text))
        return ([fragment] if fragment.content else []), timestamp, session_id

    if record_type == "reasoning" and isinstance(payload, dict):
        return (
            _fragment_from_content(
                payload.get("summary") or payload.get("content"),
                record_type=record_type,
                role="reasoning",
            ),
            timestamp,
            session_id,
        )

    if record_type == "event_msg" and isinstance(payload, dict):
        if payload.get("type") == "token_count":
            return [], timestamp, session_id
        text = _extract_text(
            payload.get("message") or payload.get("content") or payload.get("text")
        )
        fragment = TextFragment(record_type, "event", None, _normalize_text(text))
        return ([fragment] if fragment.content else []), timestamp, session_id

    return [], timestamp, session_id


def _claude_fragments(
    record: dict,
) -> tuple[list[TextFragment], str | None, str | None]:
    record_type = record.get("type")
    timestamp = record.get("timestamp")
    session_id = _record_session_id(record, record_type, record.get("message"))

    if record_type in {"user", "assistant", "system"}:
        message = (
            record.get("message") if isinstance(record.get("message"), dict) else record
        )
        role = message.get("role") or record_type
        content = message.get("content") or record.get("content")
        return (
            _fragment_from_content(content, record_type=record_type, role=role),
            timestamp,
            session_id,
        )

    return [], timestamp, session_id


def _generic_fragments(
    record: dict,
) -> tuple[list[TextFragment], str | None, str | None]:
    record_type = record.get("type")
    timestamp = record.get("timestamp")
    session_id = _record_session_id(record, record_type, record)
    content = record.get("message") or record.get("content") or record.get("text")
    return (
        _fragment_from_content(
            content, record_type=record_type, role=record.get("role")
        ),
        timestamp,
        session_id,
    )


def _record_fragments(
    record: dict,
    *,
    source: str,
) -> tuple[list[TextFragment], str | None, str | None]:
    if source in {"codex", "codex_archive"}:
        return _codex_fragments(record)
    if source == "claudecode":
        return _claude_fragments(record)
    return _generic_fragments(record)


def _chunk_source(candidate: RawFileCandidate) -> str:
    if candidate.source != "logpile_shared":
        return candidate.source
    path_text = f"{candidate.relative_path}/{candidate.path}".replace("\\", "/")
    if "/codex/" in path_text:
        return "codex"
    if "/claudecode/" in path_text:
        return "claudecode"
    return candidate.source


def infer_jsonl_source(path: Path, *, max_records: int = 20) -> str:
    """Infer the source parser for content-addressed raw JSONL objects."""
    with path.open("rb") as fh:
        for index, line in enumerate(fh):
            if index >= max_records:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue
            try:
                record = json.loads(decoded)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            if any(
                key in record
                for key in (
                    "parentUuid",
                    "uuid",
                    "sessionId",
                    "isSidechain",
                    "userType",
                    "cwd",
                )
            ):
                return "claudecode"

            record_type = record.get("record_type")
            type_value = record.get("type")
            if (
                "payload" in record
                or record_type
                or type_value
                in {
                    "session_meta",
                    "response_item",
                    "event_msg",
                    "function_call",
                    "function_call_output",
                    "reasoning",
                    "message",
                }
            ):
                return "codex"

    return "jsonl"


def _split_text(
    text: str,
    *,
    max_chars: int = MAX_TEXT_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
) -> Iterator[str]:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap_chars < 0:
        raise ValueError("overlap_chars must be non-negative")
    if len(text) <= max_chars:
        yield text
        return

    step = max(1, max_chars - min(overlap_chars, max_chars - 1))
    start = 0
    while start < len(text):
        yield text[start : start + max_chars]
        start += step


def iter_text_chunks(
    candidate: RawFileCandidate,
    *,
    max_chars: int = MAX_TEXT_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
) -> Iterator[RawTextChunk]:
    if candidate.path.suffix.lower() != ".jsonl":
        return

    source = _chunk_source(candidate)
    session_id: str | None = None
    with candidate.payload_path.open("rb") as fh:
        event_index = 0
        while True:
            byte_start = fh.tell()
            line = fh.readline()
            if not line:
                break
            byte_end = fh.tell()
            event_index += 1
            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue
            try:
                record = json.loads(decoded)
            except json.JSONDecodeError:
                record = {"type": "raw", "content": decoded}
            if not isinstance(record, dict):
                record = {"type": "raw", "content": decoded}

            fragments, timestamp, next_session_id = _record_fragments(
                record,
                source=source,
            )
            session_id = next_session_id or session_id

            for fragment_index, fragment in enumerate(fragments):
                for chunk_index, content in enumerate(
                    _split_text(
                        fragment.content,
                        max_chars=max_chars,
                        overlap_chars=overlap_chars,
                    )
                ):
                    yield RawTextChunk(
                        file_id=candidate.file_id,
                        event_index=event_index,
                        fragment_index=fragment_index,
                        chunk_index=chunk_index,
                        session_id=session_id,
                        source=source,
                        record_type=fragment.record_type,
                        role=fragment.role,
                        tool_name=fragment.tool_name,
                        timestamp_text=timestamp,
                        byte_start=byte_start,
                        byte_end=byte_end,
                        content=content,
                        content_sha256=sha256_text(content),
                    )


def r2_config_from_env(
    *,
    bucket: str | None = None,
    endpoint_url: str | None = None,
    account_id: str | None = None,
    access_key_id: str | None = None,
    secret_access_key: str | None = None,
    provider: str = "r2",
) -> R2Config:
    bucket = bucket or os.environ.get("LOGPILE_R2_BUCKET")
    endpoint_url = endpoint_url or os.environ.get("LOGPILE_R2_ENDPOINT_URL")
    account_id = account_id or os.environ.get("LOGPILE_R2_ACCOUNT_ID")
    access_key_id = (
        access_key_id
        or os.environ.get("LOGPILE_R2_ACCESS_KEY_ID")
        or os.environ.get("AWS_ACCESS_KEY_ID")
    )
    secret_access_key = (
        secret_access_key
        or os.environ.get("LOGPILE_R2_SECRET_ACCESS_KEY")
        or os.environ.get("AWS_SECRET_ACCESS_KEY")
    )

    if not bucket:
        raise ValueError("Set LOGPILE_R2_BUCKET or pass --bucket.")
    if not endpoint_url:
        if not account_id:
            raise ValueError("Set LOGPILE_R2_ACCOUNT_ID or LOGPILE_R2_ENDPOINT_URL.")
        endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"

    return R2Config(
        bucket=bucket,
        endpoint_url=endpoint_url,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        provider=provider,
    )


class S3ObjectStore:
    def __init__(self, config: R2Config):
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError as exc:
            raise RuntimeError(
                "Cloud object upload requires boto3. Install with: uv pip install -e '.[cloud]'"
            ) from exc

        self.config = config
        self._client_error = ClientError
        self.client = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
        )

    def exists(self, candidate: RawFileCandidate) -> bool:
        try:
            response = self.client.head_object(
                Bucket=self.config.bucket,
                Key=candidate.object_key,
            )
        except self._client_error as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return int(response.get("ContentLength", -1)) == candidate.size_bytes

    def upload(self, candidate: RawFileCandidate) -> bool:
        if self.exists(candidate):
            return False
        self.client.upload_file(
            str(candidate.payload_path),
            self.config.bucket,
            candidate.object_key,
            ExtraArgs={
                "ContentType": candidate.content_type,
                "Metadata": {"sha256": candidate.sha256},
            },
        )
        return True

    def download(self, candidate: RawFileCandidate, destination: Path) -> None:
        self.client.download_file(
            self.config.bucket,
            candidate.object_key,
            str(destination),
        )

    def iter_objects(self, *, prefix: str = "raw/sha256/") -> Iterator[dict]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.bucket, Prefix=prefix):
            yield from page.get("Contents", [])


class SupabaseArchive:
    def __init__(self, db_url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "Supabase/Postgres indexing requires psycopg. Install with: uv pip install -e '.[cloud]'"
            ) from exc

        self._psycopg = psycopg
        self._dict_row = dict_row
        self.db_url = db_url

    def connect(self, *, autocommit: bool = False):
        return self._psycopg.connect(
            self.db_url,
            row_factory=self._dict_row,
            autocommit=autocommit,
        )

    def ensure_schema(self, *, create_search_index: bool = True) -> None:
        with self.connect() as conn:
            sql = SUPABASE_BASE_SCHEMA_SQL
            if create_search_index:
                sql = f"{sql}\n{SUPABASE_SEARCH_INDEX_SQL}"
            for statement in _sql_statements(sql):
                conn.execute(statement)

    def ensure_search_index(self) -> None:
        with self.connect() as conn:
            conn.execute("SET statement_timeout = 0")
            conn.execute("DROP INDEX IF EXISTS idx_logpile_raw_chunks_fts")
            for statement in _sql_statements(SUPABASE_SEARCH_INDEX_SQL):
                conn.execute(statement)

    def upsert_file(
        self,
        conn,
        candidate: RawFileCandidate,
        *,
        provider: str,
        bucket: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO logpile_raw_objects (
                sha256, size_bytes, object_provider, bucket, object_key,
                content_type, uploaded_at, verified_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, now(), now(), now())
            ON CONFLICT (sha256) DO UPDATE SET
                size_bytes = EXCLUDED.size_bytes,
                object_provider = EXCLUDED.object_provider,
                bucket = EXCLUDED.bucket,
                object_key = EXCLUDED.object_key,
                content_type = EXCLUDED.content_type,
                verified_at = now(),
                updated_at = now()
            """,
            (
                candidate.sha256,
                candidate.size_bytes,
                provider,
                bucket,
                candidate.object_key,
                candidate.content_type,
            ),
        )
        conn.execute(
            """
            INSERT INTO logpile_raw_files (
                file_id, sha256, source, source_path, relative_path, filename,
                size_bytes, mtime_ns
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (file_id) DO UPDATE SET
                sha256 = EXCLUDED.sha256,
                source = EXCLUDED.source,
                source_path = EXCLUDED.source_path,
                relative_path = EXCLUDED.relative_path,
                filename = EXCLUDED.filename,
                size_bytes = EXCLUDED.size_bytes,
                mtime_ns = EXCLUDED.mtime_ns
            """,
            (
                candidate.file_id,
                candidate.sha256,
                candidate.source,
                str(candidate.path),
                candidate.relative_path,
                candidate.filename,
                candidate.size_bytes,
                candidate.mtime_ns,
            ),
        )

    def replace_chunks(
        self,
        conn,
        candidate: RawFileCandidate,
        chunks: Iterable[RawTextChunk],
        *,
        batch_size: int = 1000,
    ) -> int:
        conn.execute(
            "DELETE FROM logpile_raw_chunks WHERE file_id = %s", (candidate.file_id,)
        )
        inserted = 0
        batch: list[tuple] = []
        sql = """
            INSERT INTO logpile_raw_chunks (
                file_id, event_index, fragment_index, chunk_index, session_id,
                source, record_type, role, tool_name, timestamp_text,
                byte_start, byte_end, content, content_sha256
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """
        for chunk in chunks:
            batch.append(
                (
                    chunk.file_id,
                    chunk.event_index,
                    chunk.fragment_index,
                    chunk.chunk_index,
                    chunk.session_id,
                    chunk.source,
                    chunk.record_type,
                    chunk.role,
                    chunk.tool_name,
                    chunk.timestamp_text,
                    chunk.byte_start,
                    chunk.byte_end,
                    chunk.content,
                    chunk.content_sha256,
                )
            )
            if len(batch) >= batch_size:
                with conn.cursor() as cur:
                    cur.executemany(sql, batch)
                inserted += len(batch)
                batch.clear()
        if batch:
            with conn.cursor() as cur:
                cur.executemany(sql, batch)
            inserted += len(batch)
        conn.execute(
            "UPDATE logpile_raw_files SET indexed_at = now() WHERE file_id = %s",
            (candidate.file_id,),
        )
        return inserted

    def search(self, query: str, *, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                WITH q AS (SELECT websearch_to_tsquery('english', %s) AS query)
                SELECT
                    f.source,
                    f.relative_path,
                    f.source_path,
                    f.sha256,
                    o.object_key,
                    c.session_id,
                    c.event_index,
                    c.fragment_index,
                    c.chunk_index,
                    c.role,
                    c.tool_name,
                    c.timestamp_text,
                    left(c.content, 600) AS excerpt,
                    ts_rank_cd(to_tsvector('english', c.content), q.query) AS rank
                FROM logpile_raw_chunks c
                JOIN logpile_raw_files f ON f.file_id = c.file_id
                JOIN logpile_raw_objects o ON o.sha256 = f.sha256
                CROSS JOIN q
                WHERE c.role IS DISTINCT FROM 'tool_result'
                  AND to_tsvector('english', c.content) @@ q.query
                ORDER BY rank DESC, f.discovered_at DESC, c.event_index ASC
                LIMIT %s
                """,
                (query, limit),
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    """
                    SELECT
                        f.source,
                        f.relative_path,
                        f.source_path,
                        f.sha256,
                        o.object_key,
                        c.session_id,
                        c.event_index,
                        c.fragment_index,
                        c.chunk_index,
                        c.role,
                        c.tool_name,
                        c.timestamp_text,
                        left(c.content, 600) AS excerpt,
                        0.0 AS rank
                    FROM logpile_raw_chunks c
                    JOIN logpile_raw_files f ON f.file_id = c.file_id
                    JOIN logpile_raw_objects o ON o.sha256 = f.sha256
                    WHERE c.content ILIKE ('%%' || %s || '%%')
                    ORDER BY f.discovered_at DESC, c.event_index ASC
                    LIMIT %s
                    """,
                    (query, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def object_sha256s(self) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT sha256 FROM logpile_raw_objects").fetchall()
        return {str(row["sha256"]) for row in rows}

    def files_for_indexing(
        self,
        *,
        source: str | None = None,
        missing_only: bool = True,
        limit: int | None = None,
    ) -> list[dict]:
        clauses = [
            "o.verified_at IS NOT NULL",
            "o.content_type = 'application/x-ndjson'",
        ]
        params: list[object] = []
        if source:
            clauses.append("f.source = %s")
            params.append(source)
        if missing_only:
            clauses.append(
                """
                NOT EXISTS (
                    SELECT 1
                    FROM logpile_raw_chunks c
                    WHERE c.file_id = f.file_id
                )
                """
            )
        limit_sql = ""
        if limit is not None:
            limit_sql = "LIMIT %s"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    f.file_id,
                    f.sha256,
                    f.source,
                    f.source_path,
                    f.relative_path,
                    f.filename,
                    f.size_bytes,
                    f.mtime_ns,
                    o.object_key,
                    o.content_type
                FROM logpile_raw_files f
                JOIN logpile_raw_objects o ON o.sha256 = f.sha256
                WHERE {" AND ".join(clauses)}
                ORDER BY f.discovered_at, f.relative_path
                {limit_sql}
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> dict:
        with self.connect() as conn:
            objects = conn.execute(
                """
                SELECT
                    COUNT(*) AS count,
                    COALESCE(SUM(size_bytes), 0) AS bytes,
                    COUNT(*) FILTER (WHERE verified_at IS NOT NULL) AS verified_count,
                    COALESCE(SUM(size_bytes) FILTER (WHERE verified_at IS NOT NULL), 0) AS verified_bytes
                FROM logpile_raw_objects
                """
            ).fetchone()
            files = conn.execute(
                """
                SELECT
                    COUNT(*) AS count,
                    COALESCE(SUM(size_bytes), 0) AS bytes,
                    COUNT(DISTINCT source) AS sources
                FROM logpile_raw_files
                """
            ).fetchone()
            chunks = conn.execute(
                """
                SELECT
                    COALESCE(n_live_tup, 0)::bigint AS count
                FROM pg_stat_user_tables
                WHERE relname = 'logpile_raw_chunks'
                """
            ).fetchone()
            indexed_files = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM logpile_raw_files
                WHERE indexed_at IS NOT NULL
                """
            ).fetchone()
        return {
            "objects": dict(objects),
            "files": dict(files),
            "chunks": {
                "count": int(chunks["count"] or 0) if chunks else 0,
                "files": int(indexed_files["count"] or 0),
                "sessions": None,
            },
        }

    def resolve_session_id(self, session_id: str) -> str | None:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT session_id
                FROM logpile_raw_chunks
                WHERE session_id = %s
                   OR session_id LIKE (%s || '%%')
                ORDER BY session_id
                LIMIT 2
                """,
                (session_id, session_id),
            ).fetchall()
        if len(rows) == 1:
            return rows[0]["session_id"]
        if any(row["session_id"] == session_id for row in rows):
            return session_id
        return None

    def session_chunks(self, session_id: str, *, limit: int = 200) -> list[dict]:
        resolved = self.resolve_session_id(session_id)
        if not resolved:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    f.source,
                    f.relative_path,
                    f.source_path,
                    f.sha256,
                    o.object_key,
                    c.session_id,
                    c.event_index,
                    c.fragment_index,
                    c.chunk_index,
                    c.role,
                    c.tool_name,
                    c.timestamp_text,
                    c.byte_start,
                    c.byte_end,
                    c.content
                FROM logpile_raw_chunks c
                JOIN logpile_raw_files f ON f.file_id = c.file_id
                JOIN logpile_raw_objects o ON o.sha256 = f.sha256
                WHERE c.session_id = %s
                ORDER BY f.relative_path, c.event_index, c.fragment_index, c.chunk_index
                LIMIT %s
                """,
                (resolved, limit),
            ).fetchall()
        return [dict(row) for row in rows]


def push_backup(
    *,
    home: Path,
    db_url: str,
    storage_config: R2Config,
    db_path: Path | None = None,
    shared_dir: Path | None = None,
    include_codex_db: bool = True,
    limit: int | None = None,
    index_text: bool = True,
    missing_only: bool = False,
    create_search_index: bool = True,
    dry_run: bool = False,
) -> dict:
    if dry_run:
        plan = plan_backup(
            home=home,
            db_path=db_path,
            shared_dir=shared_dir,
            include_codex_db=include_codex_db,
            limit=limit,
        )
        return {
            "plan": plan_to_dict(plan),
            "uploaded": 0,
            "indexed_chunks": 0,
            "dry_run": True,
        }

    store = S3ObjectStore(storage_config)
    archive = SupabaseArchive(db_url)
    archive.ensure_schema(create_search_index=create_search_index)
    existing_sha256s = archive.object_sha256s() if missing_only else set()

    candidates: list[RawFileCandidate] = []
    uploaded = 0
    indexed_chunks = 0
    files_indexed = 0
    skipped_existing = 0
    seen_local_sha256s: set[str] = set()
    unique_considered = 0

    for discovered in _discover_raw_files(
        home,
        db_path=db_path,
        shared_dir=shared_dir,
        include_codex_db=include_codex_db,
    ):
        path = discovered.path
        try:
            if missing_only and _raw_suffix(path) != ".sqlite":
                probe = build_candidate(
                    path,
                    home=home,
                    source=discovered.source,
                )
                if probe.sha256 in seen_local_sha256s:
                    continue
                if probe.sha256 in existing_sha256s:
                    if limit is not None and unique_considered >= limit:
                        break
                    seen_local_sha256s.add(probe.sha256)
                    unique_considered += 1
                    skipped_existing += 1
                    continue
            with snapshot_candidate(
                path,
                home=home,
                source=discovered.source,
            ) as candidate:
                if candidate.sha256 in seen_local_sha256s:
                    continue
                if limit is not None and unique_considered >= limit:
                    break
                seen_local_sha256s.add(candidate.sha256)
                unique_considered += 1
                if missing_only and candidate.sha256 in existing_sha256s:
                    skipped_existing += 1
                    continue
                candidates.append(candidate)
                if store.upload(candidate):
                    uploaded += 1
                with archive.connect() as conn:
                    archive.upsert_file(
                        conn,
                        candidate,
                        provider=storage_config.provider,
                        bucket=storage_config.bucket,
                    )
                    if index_text and candidate.path.suffix.lower() == ".jsonl":
                        indexed_chunks += archive.replace_chunks(
                            conn,
                            candidate,
                            iter_text_chunks(candidate),
                        )
                        files_indexed += 1
                existing_sha256s.add(candidate.sha256)
        except FileNotFoundError:
            continue

    plan = BackupPlan(candidates=candidates)

    return {
        "plan": plan_to_dict(plan),
        "uploaded": uploaded,
        "indexed_chunks": indexed_chunks,
        "files_indexed": files_indexed,
        "skipped_existing": skipped_existing,
        "missing_only": missing_only,
        "search_index": "created" if create_search_index else "deferred",
        "dry_run": False,
    }


def _candidate_from_index_row(row: dict, *, payload_path: Path) -> RawFileCandidate:
    source_path = Path(row["source_path"] or row["relative_path"] or row["filename"])
    return RawFileCandidate(
        path=source_path,
        relative_path=row["relative_path"],
        source=row["source"],
        size_bytes=int(row["size_bytes"]),
        mtime_ns=int(row["mtime_ns"] or 0),
        sha256=row["sha256"],
        file_id=row["file_id"],
        object_key=row["object_key"],
        content_type=row["content_type"],
        upload_path=payload_path,
    )


def _sha256_from_object_key(object_key: str) -> str | None:
    match = re.search(r"/([0-9a-f]{64})(?:\.[^/]*)?$", object_key)
    return match.group(1) if match else None


def _candidate_from_r2_object(
    obj: dict,
    *,
    source: str,
    bucket: str,
    payload_path: Path,
) -> RawFileCandidate | None:
    object_key = str(obj.get("Key") or "")
    sha256 = _sha256_from_object_key(object_key)
    if not object_key or not sha256:
        return None

    last_modified = obj.get("LastModified")
    mtime_ns = 0
    if hasattr(last_modified, "timestamp"):
        mtime_ns = int(last_modified.timestamp() * 1_000_000_000)

    relative_path = f"r2/{object_key}"
    return RawFileCandidate(
        path=Path("r2") / bucket / object_key,
        relative_path=relative_path,
        source=source,
        size_bytes=int(obj.get("Size") or 0),
        mtime_ns=mtime_ns,
        sha256=sha256,
        file_id=file_id_for(relative_path, sha256),
        object_key=object_key,
        content_type=_content_type(Path(object_key)),
        upload_path=payload_path,
    )


def index_cloud_backup(
    *,
    db_url: str,
    storage_config: R2Config,
    source: str | None = None,
    missing_only: bool = True,
    limit: int | None = None,
) -> dict:
    store = S3ObjectStore(storage_config)
    archive = SupabaseArchive(db_url)
    archive.ensure_schema()
    rows = archive.files_for_indexing(
        source=source,
        missing_only=missing_only,
        limit=limit,
    )

    files_indexed = 0
    indexed_chunks = 0
    downloaded_bytes = 0
    skipped = 0

    for row in rows:
        suffix = (
            _raw_suffix(Path(row["source_path"] or row["filename"] or "raw.jsonl"))
            or ".jsonl"
        )
        fd, temp_name = tempfile.mkstemp(prefix="logpile-r2-index-", suffix=suffix)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            candidate = _candidate_from_index_row(row, payload_path=temp_path)
            store.download(candidate, temp_path)
            actual_size = temp_path.stat().st_size
            if actual_size != candidate.size_bytes:
                skipped += 1
                continue
            with archive.connect() as conn:
                indexed_chunks += archive.replace_chunks(
                    conn,
                    candidate,
                    iter_text_chunks(candidate),
                )
                files_indexed += 1
                downloaded_bytes += actual_size
        finally:
            temp_path.unlink(missing_ok=True)

    return {
        "candidate_files": len(rows),
        "files_indexed": files_indexed,
        "indexed_chunks": indexed_chunks,
        "downloaded_bytes": downloaded_bytes,
        "downloaded_human": format_bytes(downloaded_bytes),
        "skipped": skipped,
        "missing_only": missing_only,
        "source": source,
    }


def index_r2_objects(
    *,
    db_url: str,
    storage_config: R2Config,
    prefix: str = "raw/sha256/",
    source: str | None = None,
    missing_only: bool = True,
    limit: int | None = None,
    create_search_index: bool = True,
) -> dict:
    store = S3ObjectStore(storage_config)
    archive = SupabaseArchive(db_url)
    archive.ensure_schema(create_search_index=create_search_index)
    existing_sha256s = archive.object_sha256s() if missing_only else set()

    candidate_objects = 0
    files_indexed = 0
    indexed_chunks = 0
    downloaded_bytes = 0
    skipped = 0
    skipped_existing = 0
    skipped_source = 0

    for obj in store.iter_objects(prefix=prefix):
        object_key = str(obj.get("Key") or "")
        if not object_key.endswith(".jsonl"):
            continue
        sha256 = _sha256_from_object_key(object_key)
        if not sha256:
            skipped += 1
            continue
        if missing_only and sha256 in existing_sha256s:
            skipped_existing += 1
            continue
        if limit is not None and candidate_objects >= limit:
            break

        candidate_objects += 1
        fd, temp_name = tempfile.mkstemp(prefix="logpile-r2-direct-", suffix=".jsonl")
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            store.client.download_file(
                storage_config.bucket, object_key, str(temp_path)
            )
            actual_size = temp_path.stat().st_size
            expected_size = int(obj.get("Size") or 0)
            if actual_size != expected_size or sha256_file(temp_path) != sha256:
                skipped += 1
                continue

            inferred_source = infer_jsonl_source(temp_path)
            if source and inferred_source != source:
                skipped_source += 1
                continue

            candidate = _candidate_from_r2_object(
                obj,
                source=inferred_source,
                bucket=storage_config.bucket,
                payload_path=temp_path,
            )
            if candidate is None:
                skipped += 1
                continue

            with archive.connect() as conn:
                archive.upsert_file(
                    conn,
                    candidate,
                    provider=storage_config.provider,
                    bucket=storage_config.bucket,
                )
                indexed_chunks += archive.replace_chunks(
                    conn,
                    candidate,
                    iter_text_chunks(candidate),
                )
                files_indexed += 1
                downloaded_bytes += actual_size
            existing_sha256s.add(sha256)
        finally:
            temp_path.unlink(missing_ok=True)

    return {
        "candidate_objects": candidate_objects,
        "files_indexed": files_indexed,
        "indexed_chunks": indexed_chunks,
        "downloaded_bytes": downloaded_bytes,
        "downloaded_human": format_bytes(downloaded_bytes),
        "skipped": skipped,
        "skipped_existing": skipped_existing,
        "skipped_source": skipped_source,
        "missing_only": missing_only,
        "source": source,
        "prefix": prefix,
        "search_index": "created" if create_search_index else "deferred",
    }
