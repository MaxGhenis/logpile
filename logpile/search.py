"""Extracted-text FTS5 indexing and local session search."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, TextIO

from .db import SEARCH_INDEX_VERSION, _ensure_column, get_meta, set_meta
from .parsers import (
    SearchTranscriptReadError,
    clean_search_text,
    iter_session_search_text,
)
from .transcript_io import TranscriptScan, open_text_range, scan_transcript

STRUCTURED_FIELDS = (
    ("session_goal", "session_goal", True),
    ("session_summary", "session_summary", False),
    ("first_user_message", "first_user_message", True),
    ("repo_name", "repo_name", False),
    ("project", "project", False),
)
_STRUCTURED_LABELS = tuple(label for _column, label, _strip in STRUCTURED_FIELDS)
FIELD_LABELS = {
    "session_goal": "session goal",
    "session_summary": "session summary",
    "first_user_message": "first user message",
    "repo_name": "repository",
    "project": "project",
    "transcript_user": "user transcript",
    "transcript_assistant": "assistant transcript",
}
# Per-tier FTS5 sorter budget. Structured hits are ranked ahead of
# transcript hits structurally (two column-filtered queries), so this cap
# only bounds how deep each tier's bm25 ranking looks per round for very
# common terms; rounds deepen until membership is exact.
TRANSCRIPT_CANDIDATE_CAP = 4000
# Public corpora above this many documents fall back to the deepening loop
# instead of the exact rowid pushdown (per-rowid seeks stop being cheap).
_PUBLIC_PUSHDOWN_MAX_DOCS = 100_000
# Batch size for id lists bound into IN (...) clauses — safely below
# SQLite's 32,766 bound-variable limit.
_SQL_IN_CHUNK = 20000

# Resume point for appending transcript documents. Bytes [0, transcript_offset)
# hashed to transcript_prefix_sha256 and yielded exactly user_chunks and
# assistant_chunks documents; documents at or past those per-role counters
# came from the unterminated tail after the offset and are provisional. The
# columns are NULL unless the stored transcript documents are exactly those
# of the scanned bytes of a non-public transcript: every full replacement
# rewrites them, and nothing else touches documents without also rewriting
# them (_record_search_error and the stale trigger leave documents alone).
SEARCH_CHECKPOINT_COLUMNS = (
    ("transcript_offset", "INTEGER"),
    ("transcript_prefix_sha256", "TEXT"),
    ("user_chunks", "INTEGER"),
    ("assistant_chunks", "INTEGER"),
)
# States whose documents a checkpoint may still describe. 'stale' is what the
# sessions_search_stale trigger leaves once a sync upsert changes file_hash,
# i.e. the normal state an append arrives in; 'error' rows keep the documents
# of their last good revision because a failed replacement rolls back.
_RESUMABLE_STATUSES = frozenset({"complete", "stale", "error"})
# How often backfill re-verifies every row in Python instead of trusting the
# SQL prefilter, so a trigger bypass or an extraction change heals.
SEARCH_FULL_VERIFY_INTERVAL = timedelta(days=7)
_FULL_VERIFY_AT_KEY = "search_full_verify_at"
_FULL_VERIFY_VERSION_KEY = "search_full_verify_version"
# "<version>:<session id>" while a verification pass is under way.
_FULL_VERIFY_CURSOR_KEY = "search_full_verify_cursor"
# Session rows loaded per backfill chunk, so a deadline can stop between
# loads: loading all 94k rows of the profiling copy took 45.6 s cold, so a
# chunk is about 1 s cold.
_BACKFILL_CHUNK = 2000


class SearchIndexUnavailable(RuntimeError):
    """The local extracted-text index has not been built yet."""


# Mirrors the sessions_search_stale trigger's watch list. Replacement
# re-checks these inside its savepoint so a session mutation committed
# between the row snapshot and the state write can never be clobbered by a
# stale 'complete' revision.
_SEARCH_INPUT_COLUMNS = (
    "source",
    "file_hash",
    "session_goal",
    "session_summary",
    "first_user_message",
    "repo_name",
    "project",
    "visibility",
    "reviewed_sha256",
    "reviewed_artifact_path",
    "publication_metadata_sha256",
    "reviewed_metadata_sha256",
)


def _search_inputs_drifted(conn: sqlite3.Connection, row: Any) -> bool:
    current = conn.execute(
        "SELECT {} FROM sessions WHERE session_id = ?".format(
            ", ".join(_SEARCH_INPUT_COLUMNS)
        ),
        (row["session_id"],),
    ).fetchone()
    if current is None:
        return True
    return any(
        current[column] != _value(row, column) for column in _SEARCH_INPUT_COLUMNS
    )


@dataclass(frozen=True)
class SearchBackfillStats:
    scanned: int
    indexed: int
    skipped: int
    missing: int
    errors: int
    indexed_bytes: int
    elapsed_seconds: float
    # Stale rows left for a later call because the deadline passed.
    deferred: int = 0


@dataclass(frozen=True)
class _SearchCheckpoint:
    offset: int
    prefix_sha256: str
    user_chunks: int
    assistant_chunks: int


def ensure_search_checkpoint_columns(conn: sqlite3.Connection) -> None:
    """Add the append-resume columns to session_search_state if missing.

    ADD COLUMN with a NULL default only edits the schema, so existing rows
    read as "no checkpoint" and take one full replacement before resuming.
    """
    for column, spec in SEARCH_CHECKPOINT_COLUMNS:
        _ensure_column(conn, "session_search_state", column, spec)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return None


def _structured_values(row: Any) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for column, label, strip_preamble in STRUCTURED_FIELDS:
        text = clean_search_text(
            _value(row, column),
            strip_preamble=strip_preamble,
        )
        if text:
            values.append((label, text))
    return values


def search_metadata_hash(row: Any) -> str:
    """Fingerprint exactly the structured values stored in the FTS index."""
    digest = hashlib.sha256()
    for label, text in _structured_values(row):
        for value in (label, text):
            encoded = value.encode("utf-8", errors="replace")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _insert_document(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    field_label: str,
    chunk_index: int,
    structured_text: str | None = None,
    transcript_text: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO session_search_documents (
            session_id, field_label, chunk_index,
            structured_text, transcript_text
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            session_id,
            field_label,
            chunk_index,
            structured_text,
            transcript_text,
        ),
    )


def _insert_structured_documents(conn: sqlite3.Connection, row: Any) -> None:
    for field_label, text in _structured_values(row):
        _insert_document(
            conn,
            session_id=row["session_id"],
            field_label=field_label,
            chunk_index=0,
            structured_text=text,
        )


def _sync_structured_documents(conn: sqlite3.Connection, row: Any) -> None:
    """Rewrite the structured documents only if they differ from ``row``.

    Compares the stored rows themselves rather than trusting the state's
    metadata_hash, which _record_search_error advances without touching
    documents. On any difference all of them are replaced in field order, so
    their relative id order (the within-session tiebreak) matches a full
    replacement.
    """
    session_id = row["session_id"]
    placeholders = ", ".join("?" for _ in _STRUCTURED_LABELS)
    stored = [
        tuple(document)
        for document in conn.execute(
            f"""
            SELECT field_label, chunk_index, structured_text, transcript_text
            FROM session_search_documents
            WHERE session_id = ? AND field_label IN ({placeholders})
            ORDER BY id
            """,
            (session_id, *_STRUCTURED_LABELS),
        )
    ]
    wanted = [(label, 0, text, None) for label, text in _structured_values(row)]
    if stored == wanted:
        return
    conn.execute(
        "DELETE FROM session_search_documents "
        f"WHERE session_id = ? AND field_label IN ({placeholders})",
        (session_id, *_STRUCTURED_LABELS),
    )
    _insert_structured_documents(conn, row)


def _index_transcript_range(
    conn: sqlite3.Connection,
    row: Any,
    handle: IO[bytes],
    start: int,
    end: int,
    counters: dict[str, int],
) -> None:
    """Append documents for bytes [start, end), continuing ``counters``.

    ``start`` sits on a line boundary, and extraction is stateless per
    record, so the documents of consecutive ranges concatenate to exactly
    the documents of the whole span.
    """
    if end <= start:
        return
    session_id = row["session_id"]
    with open_text_range(handle, start, end) as stream:
        for role, text in iter_session_search_text(stream, row["source"]):
            chunk_index = counters[role]
            counters[role] += 1
            _insert_document(
                conn,
                session_id=session_id,
                field_label=f"transcript_{role}",
                chunk_index=chunk_index,
                transcript_text=text,
            )
        # The range reader returns EOF early when the file is shorter than
        # the scan said; that would silently index a different revision.
        consumed = stream.buffer.raw.tell()
    if consumed != end - start:
        raise SearchTranscriptReadError(
            f"Transcript shorter than its scan ({start + consumed} < {end} bytes): "
            f"{getattr(handle, 'name', session_id)}"
        )


def _write_search_state(
    conn: sqlite3.Connection,
    row: Any,
    *,
    artifact_hash: str | None,
    transcript_status: str,
    last_error: str | None,
    checkpoint: _SearchCheckpoint | None,
) -> None:
    conn.execute(
        """
        INSERT INTO session_search_state (
            session_id, search_version, file_hash, artifact_hash, metadata_hash,
            transcript_status, last_error, indexed_at, transcript_offset,
            transcript_prefix_sha256, user_chunks, assistant_chunks
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            search_version = excluded.search_version,
            file_hash = excluded.file_hash,
            artifact_hash = excluded.artifact_hash,
            metadata_hash = excluded.metadata_hash,
            transcript_status = excluded.transcript_status,
            last_error = excluded.last_error,
            indexed_at = excluded.indexed_at,
            transcript_offset = excluded.transcript_offset,
            transcript_prefix_sha256 = excluded.transcript_prefix_sha256,
            user_chunks = excluded.user_chunks,
            assistant_chunks = excluded.assistant_chunks
        """,
        (
            row["session_id"],
            SEARCH_INDEX_VERSION,
            row["file_hash"],
            artifact_hash,
            search_metadata_hash(row),
            transcript_status,
            (last_error or "")[:1000] or None,
            _now_iso(),
            checkpoint.offset if checkpoint else None,
            checkpoint.prefix_sha256 if checkpoint else None,
            checkpoint.user_chunks if checkpoint else None,
            checkpoint.assistant_chunks if checkpoint else None,
        ),
    )


def remove_session_search_index(conn: sqlite3.Connection, session_id: str) -> None:
    """Remove all search rows/state for one session using the B-tree index."""
    conn.execute(
        "DELETE FROM session_search_documents WHERE session_id = ?",
        (session_id,),
    )
    conn.execute(
        "DELETE FROM session_search_state WHERE session_id = ?",
        (session_id,),
    )


def _session_row(conn: sqlite3.Connection, session_id: str) -> Any:
    return conn.execute(
        """
        SELECT
            s.*,
            COALESCE(u.display_name, s.username) AS display_name,
            u.bio AS bio,
            u.avatar_url AS avatar_url
        FROM sessions AS s
        LEFT JOIN users AS u ON u.username = s.username
        WHERE s.session_id = ?
        """,
        (session_id,),
    ).fetchone()


def _replace_public_session_search_index(
    conn: sqlite3.Connection,
    row: Any,
    *,
    shared_dir: Path | None,
    last_error: str | None,
) -> str:
    session_id = row["session_id"]
    if shared_dir is None:
        raise SearchTranscriptReadError(
            "Public search indexing requires the managed shared directory"
        )
    # Public search is itself a publication surface. Read from the same
    # O_NOFOLLOW file description whose bytes and metadata pass the B7
    # publication checks; never substitute a mutable source/shared path.
    from .publish import open_verified_public_artifact

    with open_verified_public_artifact(row, shared_dir=shared_dir) as stream:
        if stream is None:
            raise SearchTranscriptReadError(
                f"No verified public artifact for search indexing: {session_id}"
            )
        return _replace_session_search_documents(
            conn,
            row,
            transcript_source=stream,
            transcript_status="complete",
            artifact_hash=row["reviewed_sha256"],
            last_error=last_error,
        )


def replace_session_search_index(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    transcript_path: Path | None,
    shared_dir: Path | None = None,
    transcript_status: str | None = None,
    last_error: str | None = None,
) -> str:
    """Atomically replace one session's structured and transcript documents.

    Transcript iteration is record-streaming. A late read failure rolls the
    savepoint back, so a partial replacement can never displace the last
    complete index revision. Without a scan of the bytes read, the append
    checkpoint is cleared; update_session_search_index records one.
    """
    row = _session_row(conn, session_id)
    if row is None:
        remove_session_search_index(conn, session_id)
        return "removed"

    if row["visibility"] == "public":
        return _replace_public_session_search_index(
            conn, row, shared_dir=shared_dir, last_error=last_error
        )

    effective_status = transcript_status or (
        "complete" if transcript_path is not None else "metadata_only"
    )
    return _replace_session_search_documents(
        conn,
        row,
        transcript_source=transcript_path,
        transcript_status=effective_status,
        artifact_hash=None,
        last_error=last_error,
    )


def _replace_session_search_documents(
    conn: sqlite3.Connection,
    row: Any,
    *,
    transcript_source: Path | TextIO | None,
    transcript_status: str,
    artifact_hash: str | None,
    last_error: str | None,
) -> str:
    """Replace canonical documents after the transcript source is trusted."""
    session_id = row["session_id"]

    conn.execute("SAVEPOINT replace_session_search_index")
    try:
        conn.execute(
            "DELETE FROM session_search_documents WHERE session_id = ?",
            (session_id,),
        )
        _insert_structured_documents(conn, row)

        role_indexes = {"user": 0, "assistant": 0}
        if transcript_source is not None:
            for role, text in iter_session_search_text(
                transcript_source,
                row["source"],
            ):
                field_label = f"transcript_{role}"
                chunk_index = role_indexes[role]
                role_indexes[role] += 1
                _insert_document(
                    conn,
                    session_id=session_id,
                    field_label=field_label,
                    chunk_index=chunk_index,
                    transcript_text=text,
                )

        _write_search_state(
            conn,
            row,
            artifact_hash=artifact_hash,
            transcript_status=transcript_status,
            last_error=last_error,
            checkpoint=None,
        )
        if _search_inputs_drifted(conn, row):
            raise SearchTranscriptReadError(
                "Session inputs changed while indexing "
                f"{session_id}; replacement deferred"
            )
        conn.execute("RELEASE SAVEPOINT replace_session_search_index")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT replace_session_search_index")
        conn.execute("RELEASE SAVEPOINT replace_session_search_index")
        raise
    return transcript_status


def search_checkpoint_offset(conn: sqlite3.Connection, session_id: str) -> int | None:
    """Byte offset whose prefix hash update_session_search_index will need.

    Pass it in ``scan_transcript(..., prefix_offsets=[...])``; a scan
    without it makes the next update a full replacement.
    """
    state = conn.execute(
        "SELECT transcript_offset FROM session_search_state WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return None if state is None else state[0]


def _resumable_checkpoint(
    conn: sqlite3.Connection, session_id: str, scan: TranscriptScan
) -> _SearchCheckpoint | None:
    """The stored checkpoint, if the new bytes provably extend its prefix."""
    state = conn.execute(
        """
        SELECT
            search_version, file_hash, artifact_hash, transcript_status,
            transcript_offset, transcript_prefix_sha256,
            user_chunks, assistant_chunks
        FROM session_search_state
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if state is None:
        return None
    if not (
        state["search_version"] == SEARCH_INDEX_VERSION
        and state["file_hash"]
        # Documents indexed from a reviewed public artifact never resume.
        and state["artifact_hash"] is None
        and state["transcript_status"] in _RESUMABLE_STATUSES
    ):
        return None
    offset = state["transcript_offset"]
    prefix_sha256 = state["transcript_prefix_sha256"]
    counters = {
        "user": state["user_chunks"],
        "assistant": state["assistant_chunks"],
    }
    if offset is None or any(count is None for count in counters.values()):
        return None
    # Implied by a matching prefix (the offset sits just past a terminator,
    # so line_end >= offset); kept so a range can never run backwards.
    if offset > scan.line_end or not scan.prefix_matches(offset, prefix_sha256):
        return None
    for role, count in counters.items():
        # Cheap guard against documents removed out of band: the last
        # settled chunk of each role must still be present.
        highest = conn.execute(
            """
            SELECT MAX(chunk_index) FROM session_search_documents
            WHERE session_id = ? AND field_label = ? AND chunk_index < ?
            """,
            (session_id, f"transcript_{role}", count),
        ).fetchone()[0]
        if (-1 if highest is None else highest) != count - 1:
            return None
    return _SearchCheckpoint(
        offset=offset,
        prefix_sha256=prefix_sha256,
        user_chunks=counters["user"],
        assistant_chunks=counters["assistant"],
    )


def _update_session_search_documents(
    conn: sqlite3.Connection,
    row: Any,
    *,
    handle: IO[bytes],
    scan: TranscriptScan,
) -> str:
    """Bring a non-public session's documents to exactly bytes [0, scan.size).

    Resumes from the stored checkpoint when the scan proves those bytes
    extend it; otherwise replaces everything. Either way the result equals a
    full replacement on the same bytes, and a new checkpoint at
    ``scan.line_end`` is recorded.
    """
    session_id = row["session_id"]
    conn.execute("SAVEPOINT replace_session_search_index")
    try:
        resume = _resumable_checkpoint(conn, session_id, scan)
        if resume is None:
            conn.execute(
                "DELETE FROM session_search_documents WHERE session_id = ?",
                (session_id,),
            )
            _insert_structured_documents(conn, row)
            counters = {"user": 0, "assistant": 0}
            start = 0
        else:
            # Retract the provisional documents of the old unterminated
            # tail; its line is re-read from the offset below.
            for role, count in (
                ("user", resume.user_chunks),
                ("assistant", resume.assistant_chunks),
            ):
                conn.execute(
                    """
                    DELETE FROM session_search_documents
                    WHERE session_id = ? AND field_label = ? AND chunk_index >= ?
                    """,
                    (session_id, f"transcript_{role}", count),
                )
            _sync_structured_documents(conn, row)
            counters = {
                "user": resume.user_chunks,
                "assistant": resume.assistant_chunks,
            }
            start = resume.offset

        _index_transcript_range(conn, row, handle, start, scan.line_end, counters)
        checkpoint = _SearchCheckpoint(
            offset=scan.line_end,
            prefix_sha256=scan.line_end_sha256,
            user_chunks=counters["user"],
            assistant_chunks=counters["assistant"],
        )
        # A final line without its terminator is indexed now, like a full
        # replacement would, but past the checkpoint so the next update
        # retracts it and reads the completed line instead.
        _index_transcript_range(conn, row, handle, scan.line_end, scan.size, counters)

        _write_search_state(
            conn,
            row,
            artifact_hash=None,
            transcript_status="complete",
            last_error=None,
            checkpoint=checkpoint,
        )
        if _search_inputs_drifted(conn, row):
            raise SearchTranscriptReadError(
                "Session inputs changed while indexing "
                f"{session_id}; replacement deferred"
            )
        conn.execute("RELEASE SAVEPOINT replace_session_search_index")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT replace_session_search_index")
        conn.execute("RELEASE SAVEPOINT replace_session_search_index")
        raise
    return "complete"


def update_session_search_index(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    transcript_path: Path,
    scan: TranscriptScan,
    shared_dir: Path | None = None,
) -> str:
    """Index one session's transcript, appending when only new bytes arrived.

    The caller guarantees that the first ``scan.size`` bytes of
    ``transcript_path`` are exactly the bytes ``scan`` hashed (for sync: the
    sessions row's ``file_hash`` equals ``scan.sha256`` and the path is the
    verified shared copy), and should request
    ``search_checkpoint_offset(conn, session_id)`` in the scan's
    ``prefix_offsets``. Bytes past ``scan.size`` are never read.

    Afterwards the documents are exactly what replace_session_search_index
    would produce from those bytes. Only documents at or past the stored
    checkpoint are rewritten when the scan proves the checkpoint prefix is
    unchanged, and structured documents only when their values changed.
    Public rows keep the verified-artifact full replacement. Raises
    SearchTranscriptReadError (with the previous revision intact) when the
    transcript cannot be read or the session changed while indexing.
    """
    row = _session_row(conn, session_id)
    if row is None:
        remove_session_search_index(conn, session_id)
        return "removed"
    if row["visibility"] == "public":
        return _replace_public_session_search_index(
            conn, row, shared_dir=shared_dir, last_error=None
        )
    try:
        handle = open(transcript_path, "rb")  # noqa: SIM115 - closed by `with handle`
    except OSError as exc:
        raise SearchTranscriptReadError(
            f"Could not open transcript for search indexing: {transcript_path}"
        ) from exc
    with handle:
        return _update_session_search_documents(conn, row, handle=handle, scan=scan)


def _index_backfill_transcript(conn: sqlite3.Connection, row: Any, path: Path) -> None:
    """Scan ``path`` and index exactly the scanned bytes, resuming if able."""
    with open(path, "rb") as handle:
        opened = os.fstat(handle.fileno())
        scan = scan_transcript(
            path, prefix_offsets=[_value(row, "indexed_transcript_offset")]
        )
        # Read through the descriptor opened before the scan; if the path was
        # swapped in between, the scanned bytes are not the ones we hold.
        if (scan.dev, scan.ino) != (opened.st_dev, opened.st_ino):
            raise SearchTranscriptReadError(
                f"Transcript replaced while indexing: {path}"
            )
        _update_session_search_documents(conn, row, handle=handle, scan=scan)


def _record_search_error(
    conn: sqlite3.Connection,
    row: Any,
    *,
    status: str,
    error: str,
) -> None:
    # Documents are untouched (the failed replacement rolled back), so the
    # append checkpoint columns keep describing them. Only an existing
    # session gets a state row, so this can never create an orphan.
    conn.execute(
        """
        INSERT INTO session_search_state (
            session_id, search_version, file_hash, artifact_hash, metadata_hash,
            transcript_status, last_error, indexed_at
        )
        SELECT ?, ?, ?, NULL, ?, ?, ?, ?
        WHERE EXISTS (SELECT 1 FROM sessions WHERE session_id = ?)
        ON CONFLICT(session_id) DO UPDATE SET
            search_version = excluded.search_version,
            file_hash = excluded.file_hash,
            artifact_hash = NULL,
            metadata_hash = excluded.metadata_hash,
            transcript_status = excluded.transcript_status,
            last_error = excluded.last_error,
            indexed_at = excluded.indexed_at
        """,
        (
            row["session_id"],
            SEARCH_INDEX_VERSION,
            row["file_hash"],
            search_metadata_hash(row),
            status,
            error[:1000],
            _now_iso(),
            row["session_id"],
        ),
    )


def _transcript_path(row: Any) -> Path | None:
    for column in ("shared_path", "source_path"):
        raw_path = _value(row, column)
        if not raw_path:
            continue
        path = Path(str(raw_path))
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _state_is_current(row: Any) -> bool:
    current = bool(
        _value(row, "indexed_search_version") == SEARCH_INDEX_VERSION
        and _value(row, "indexed_file_hash") == _value(row, "file_hash")
        and _value(row, "indexed_metadata_hash") == search_metadata_hash(row)
        and _value(row, "indexed_transcript_status") == "complete"
    )
    if not current:
        return False
    if _value(row, "visibility") != "public":
        return True
    reviewed_hash = _value(row, "reviewed_sha256")
    return bool(
        reviewed_hash
        and _value(row, "indexed_artifact_hash") == reviewed_hash
        and _value(row, "publication_state") == "reviewed"
        and _value(row, "publication_metadata_sha256")
        == _value(row, "reviewed_metadata_sha256")
    )


_BACKFILL_ROWS_SQL = """
    SELECT
        s.*,
        COALESCE(u.display_name, s.username) AS display_name,
        u.bio AS bio,
        u.avatar_url AS avatar_url,
        st.search_version AS indexed_search_version,
        st.file_hash AS indexed_file_hash,
        st.artifact_hash AS indexed_artifact_hash,
        st.metadata_hash AS indexed_metadata_hash,
        st.transcript_status AS indexed_transcript_status,
        st.transcript_offset AS indexed_transcript_offset
    FROM sessions AS s
    LEFT JOIN users AS u ON u.username = s.username
    LEFT JOIN session_search_state AS st
      ON st.session_id = s.session_id
"""


def _stale_candidate_ids(conn: sqlite3.Connection) -> list[str]:
    """Ids of the sessions whose index is not current, without hashing rows.

    A row is current (_state_is_current) when its state has the current
    version, status 'complete', its file_hash, and its metadata hash, plus
    the publication checks for public rows. 'complete' is only written
    inside a savepoint whose final _search_inputs_drifted check proves every
    sessions_search_stale input (source, file_hash, and the five
    STRUCTURED_FIELDS columns session_goal, session_summary,
    first_user_message, repo_name, project, which are everything
    search_metadata_hash reads) still equals the snapshot it indexed. After
    that commit, the trigger turns the status to 'stale' on any null-safe
    change of those columns. So while the trigger is intact, file_hash and
    metadata drift always surface as a non-'complete' status, and the
    stale rows are exactly:

    - sessions without a state row (anti-join over both primary keys);
    - state rows with another version or a non-'complete' status;
    - public rows failing the publication checks, which _state_is_current
      applies in Python and this repeats in SQL (publication_state is not a
      trigger input, so it cannot be left to the status).

    Trigger bypasses (SQL run with the trigger dropped, or INSERT OR REPLACE
    on sessions, which deletes without firing triggers; logpile writes
    sessions only through UPDATE and ON CONFLICT DO UPDATE) and extraction
    changes without a SEARCH_INDEX_VERSION bump are healed by the periodic
    full verification in backfill_search_index.
    """
    candidates: set[str] = set()
    candidates.update(
        row[0]
        for row in conn.execute(
            """
            SELECT s.session_id FROM sessions AS s
            WHERE NOT EXISTS (
                SELECT 1 FROM session_search_state AS st
                WHERE st.session_id = s.session_id
            )
            """
        )
    )
    candidates.update(
        row[0]
        for row in conn.execute(
            """
            SELECT st.session_id FROM session_search_state AS st
            WHERE (
                st.search_version IS NOT ?
                OR st.transcript_status IS NOT 'complete'
            )
              AND EXISTS (
                SELECT 1 FROM sessions AS s WHERE s.session_id = st.session_id
              )
            """,
            (SEARCH_INDEX_VERSION,),
        )
    )
    candidates.update(
        row[0]
        for row in conn.execute(
            """
            SELECT s.session_id
            FROM sessions AS s
            JOIN session_search_state AS st ON st.session_id = s.session_id
            WHERE s.visibility = 'public'
              AND NOT (
                COALESCE(s.reviewed_sha256, '') != ''
                AND st.artifact_hash IS s.reviewed_sha256
                AND s.publication_state IS 'reviewed'
                AND s.publication_metadata_sha256 IS s.reviewed_metadata_sha256
              )
            """
        )
    )
    return sorted(candidates)


def _candidate_rows(conn: sqlite3.Connection, session_ids: list[str]) -> list[Any]:
    rows: list[Any] = []
    # Ids are sorted, so per-chunk ordering keeps the whole list in order.
    for start in range(0, len(session_ids), _SQL_IN_CHUNK):
        batch = session_ids[start : start + _SQL_IN_CHUNK]
        placeholders = ", ".join("?" for _ in batch)
        rows.extend(
            conn.execute(
                f"{_BACKFILL_ROWS_SQL} WHERE s.session_id IN ({placeholders}) "
                "ORDER BY s.session_id",
                batch,
            ).fetchall()
        )
    return rows


def _id_range_sql(column: str, after: str | None, through: str | None) -> str:
    """Bounds (after, through] on a TEXT id; None leaves that side open."""
    # Every TEXT value sorts at or after '', so the open lower bound still
    # lets SQLite seek the index instead of scanning.
    clauses = [f"{column} > :after" if after is not None else f"{column} >= ''"]
    if through is not None:
        clauses.append(f"{column} <= :through")
    return " AND ".join(clauses)


def _verification_chunk(conn: sqlite3.Connection, after: str | None) -> list[Any]:
    return conn.execute(
        f"{_BACKFILL_ROWS_SQL} WHERE {_id_range_sql('s.session_id', after, None)} "
        "ORDER BY s.session_id LIMIT :limit",
        {"after": after, "limit": _BACKFILL_CHUNK},
    ).fetchall()


def _full_verify_due(conn: sqlite3.Connection, now: datetime) -> bool:
    if get_meta(conn, _FULL_VERIFY_VERSION_KEY) != str(SEARCH_INDEX_VERSION):
        return True
    try:
        verified_at = datetime.fromisoformat(get_meta(conn, _FULL_VERIFY_AT_KEY) or "")
    except ValueError:
        return True
    if verified_at.tzinfo is None:
        return True
    # A timestamp in the future (clock change) also forces a pass.
    return not (now - SEARCH_FULL_VERIFY_INTERVAL < verified_at <= now)


def _verification_cursor(conn: sqlite3.Connection) -> tuple[bool, str | None]:
    """Whether a verification pass is under way, and the last id it covered."""
    raw = get_meta(conn, _FULL_VERIFY_CURSOR_KEY)
    if raw is None:
        return False, None
    version, _, session_id = raw.partition(":")
    if version != str(SEARCH_INDEX_VERSION):
        # A pass begun under another index version starts over.
        return True, None
    return True, session_id


def _heal_orphan_search_rows(
    conn: sqlite3.Connection,
    *,
    after: str | None = None,
    through: str | None = None,
) -> int:
    """Remove search rows in session id range (after, through] that have no
    session; return how many ids were removed.

    Orphans cannot accumulate through logpile itself: every DELETE FROM
    sessions fires sessions_search_cleanup, which deletes that session's
    documents and state; the only UNIQUE key on sessions is session_id, so a
    REPLACE conflict re-inserts the same id; documents are only written
    inside a savepoint whose _search_inputs_drifted check fails when the
    session row is gone; and _record_search_error only inserts for an
    existing session. Orphans are also invisible to search, whose queries
    join session_catalog (a view over sessions). So this runs only with the
    periodic full verification, as a heal for writes that bypassed the
    trigger. The distinct document session ids come from a loose index scan
    of idx_session_search_documents_session (one seek per session) rather
    than a scan of the documents table and its text.
    """
    bounds = {"after": after, "through": through}
    continue_while = "" if through is None else "AND indexed.session_id < :through"
    orphan_ids = {
        row[0]
        for row in conn.execute(
            f"""
            WITH RECURSIVE indexed(session_id) AS (
                SELECT (
                    SELECT session_id FROM session_search_documents
                    WHERE {_id_range_sql("session_id", after, None)}
                    ORDER BY session_id
                    LIMIT 1
                )
                UNION ALL
                SELECT (
                    SELECT d.session_id FROM session_search_documents AS d
                    WHERE d.session_id > indexed.session_id
                    ORDER BY d.session_id
                    LIMIT 1
                )
                FROM indexed
                WHERE indexed.session_id IS NOT NULL {continue_while}
            )
            SELECT session_id FROM indexed
            WHERE session_id IS NOT NULL
              AND {_id_range_sql("session_id", after, through)}
              AND NOT EXISTS (
                SELECT 1 FROM sessions WHERE sessions.session_id = indexed.session_id
              )
            """,
            bounds,
        )
    }
    orphan_ids.update(
        row[0]
        for row in conn.execute(
            f"""
            SELECT session_id FROM session_search_state
            WHERE {_id_range_sql("session_id", after, through)}
              AND NOT EXISTS (
                SELECT 1 FROM sessions
                WHERE sessions.session_id = session_search_state.session_id
              )
            """,
            bounds,
        )
    )
    for session_id in sorted(orphan_ids):
        remove_session_search_index(conn, session_id)
    return len(orphan_ids)


class _BackfillRun:
    """Counters and batch commits shared by the backfill passes."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        shared_dir: Path | None,
        batch_size: int,
        verbose: bool,
        deadline: float | None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.conn = conn
        self.shared_dir = shared_dir
        self.batch_size = max(1, batch_size)
        self.verbose = verbose
        self.deadline = deadline
        self.should_stop = should_stop
        self.scanned = self.indexed = self.skipped = self.missing = 0
        self.errors = self.indexed_bytes = self.deferred = 0
        self.attempted_since_commit = 0
        # Rows already attempted by this call; a row left 'missing' or
        # 'error' is not current, and verification must not retry it.
        self.attempted: set[str] = set()

    def deadline_passed(self) -> bool:
        if self.should_stop is not None and self.should_stop():
            return True
        return self.deadline is not None and time.monotonic() >= self.deadline

    def must_stop_before_row(self) -> bool:
        """``should_stop`` ends the run before any row; the deadline only
        after this call has attempted one, so each call makes progress while
        overrunning the deadline by at most one transcript."""
        if self.should_stop is not None and self.should_stop():
            return True
        return bool(self.attempted) and self.deadline_passed()

    def commit(self) -> None:
        self.conn.commit()
        if self.attempted_since_commit:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.attempted_since_commit = 0

    def _defer_rest(self, rows: list[Any], start: int) -> None:
        self.deferred += sum(
            1
            for rest in rows[start:]
            if rest["session_id"] not in self.attempted and not _state_is_current(rest)
        )

    def process(self, rows: list[Any]) -> int | None:
        """Index the stale rows in order.

        Returns None when every row was handled, or the position of the last
        handled row (-1 for none) when the deadline or ``should_stop`` ended
        the run. Both are checked before every row (see must_stop_before_row).
        """
        for position, row in enumerate(rows):
            if row["session_id"] in self.attempted:
                continue
            self.scanned += 1
            if _state_is_current(row):
                self.skipped += 1
                continue
            if self.must_stop_before_row():
                if self.attempted_since_commit:
                    self.commit()
                self._defer_rest(rows, position)
                return position - 1
            self.attempted.add(row["session_id"])
            self._index_row(row)
            self.attempted_since_commit += 1
            if self.attempted_since_commit >= self.batch_size:
                self.commit()
                if self.deadline_passed():
                    self._defer_rest(rows, position + 1)
                    return position
            done = self.indexed + self.missing + self.errors
            if self.verbose and done % 250 == 0:
                print(
                    f"  Search backfill: {done}/{self.scanned} refreshed",
                    flush=True,
                )
        return None

    def _index_row(self, row: Any) -> None:
        conn = self.conn
        # Python's sqlite3 does not open a transaction for SAVEPOINT, so an
        # outermost per-session savepoint would autocommit on release. Start
        # the batch transaction explicitly so the periodic commit is a real
        # batch boundary.
        if not conn.in_transaction:
            conn.execute("BEGIN")

        if row["visibility"] == "public":
            try:
                replace_session_search_index(
                    conn,
                    row["session_id"],
                    transcript_path=None,
                    shared_dir=self.shared_dir,
                )
            except (OSError, SearchTranscriptReadError) as exc:
                self.errors += 1
                _record_search_error(conn, row, status="error", error=str(exc))
            else:
                self.indexed += 1
                self.indexed_bytes += max(0, int(row["file_size"] or 0))
            return

        path = _transcript_path(row)
        if path is None:
            self.missing += 1
            replace_session_search_index(
                conn,
                row["session_id"],
                transcript_path=None,
                transcript_status="missing",
                last_error="No readable source_path or shared_path",
            )
            return
        try:
            _index_backfill_transcript(conn, row, path)
        except (OSError, SearchTranscriptReadError) as exc:
            self.errors += 1
            _record_search_error(conn, row, status="error", error=str(exc))
        else:
            self.indexed += 1
            self.indexed_bytes += max(0, int(row["file_size"] or 0))


def backfill_search_index(
    conn: sqlite3.Connection,
    *,
    shared_dir: Path | None = None,
    batch_size: int = 50,
    verbose: bool = False,
    deadline: float | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> SearchBackfillStats:
    """Resume the extracted-text index across every durable session row.

    First the rows _stale_candidate_ids selects are indexed, loaded
    _BACKFILL_CHUNK at a time. Then, every
    SEARCH_FULL_VERIFY_INTERVAL or after a SEARCH_INDEX_VERSION change, a
    verification pass re-checks every row in session id order, _BACKFILL_CHUNK
    rows at a time, and heals orphans in each chunk's id range; its cursor
    is committed after each chunk, so a pass spans as many calls as it needs.

    ``deadline`` is a time.monotonic() value. Once it has passed, no further
    transcript is indexed after the first this call attempts, so each call
    makes progress and overruns by at most one transcript. ``should_stop``
    (a termination signal or low disk in sync) stops before any further
    transcript. Finished work is committed and verification keeps its
    cursor. Stale rows known to be left behind are reported as ``deferred``.
    """
    started = time.monotonic()
    now = datetime.now(UTC)
    run = _BackfillRun(
        conn,
        shared_dir=shared_dir,
        batch_size=batch_size,
        verbose=verbose,
        deadline=deadline,
        should_stop=should_stop,
    )
    candidates = _stale_candidate_ids(conn)
    stopped = None
    for start in range(0, len(candidates), _BACKFILL_CHUNK):
        stopped = run.process(
            _candidate_rows(conn, candidates[start : start + _BACKFILL_CHUNK])
        )
        if stopped is not None:
            # Every candidate is stale, so the unloaded ones are deferred too.
            run.deferred += len(candidates[start + _BACKFILL_CHUNK :])
            break
    in_progress, cursor = _verification_cursor(conn)
    if (
        stopped is None
        and not (run.should_stop is not None and run.should_stop())
        and (in_progress or _full_verify_due(conn, now))
    ):
        while True:
            chunk = _verification_chunk(conn, after=cursor)
            stopped = run.process(chunk)
            if stopped == -1:
                # Stopped before handling any row of this chunk.
                break
            if stopped is not None:
                # Heal up to the last handled row; the rest of the chunk is
                # re-read from the cursor next time.
                through = chunk[stopped]["session_id"]
            elif len(chunk) == _BACKFILL_CHUNK:
                through = chunk[-1]["session_id"]
            else:
                through = None
            _heal_orphan_search_rows(conn, after=cursor, through=through)
            if through is None:
                set_meta(conn, _FULL_VERIFY_AT_KEY, now.isoformat())
                set_meta(conn, _FULL_VERIFY_VERSION_KEY, str(SEARCH_INDEX_VERSION))
                set_meta(conn, _FULL_VERIFY_CURSOR_KEY, None)
                run.commit()
                break
            cursor = through
            set_meta(conn, _FULL_VERIFY_CURSOR_KEY, f"{SEARCH_INDEX_VERSION}:{cursor}")
            run.commit()
            if stopped is not None or run.deadline_passed():
                break
    if run.attempted_since_commit:
        run.commit()

    return SearchBackfillStats(
        scanned=run.scanned,
        indexed=run.indexed,
        skipped=run.skipped,
        missing=run.missing,
        errors=run.errors,
        indexed_bytes=run.indexed_bytes,
        elapsed_seconds=time.monotonic() - started,
        deferred=run.deferred,
    )


def _quoted_fts_phrase(query: str) -> str:
    normalized = " ".join((query or "").split())
    if not normalized:
        return ""
    return f'"{normalized.replace(chr(34), chr(34) * 2)}"'


# Shared eligibility predicate. Every query that returns document text or
# metadata — candidates AND snippets — must apply it; binding a snippet by
# bare rowid would trust rowid reuse across writes. Bound params: search
# version, then the public-mode flag.
_ELIGIBILITY_SQL = """
          st.search_version = ?
          AND st.file_hash IS s.file_hash
          AND st.transcript_status IN ('complete', 'metadata_only', 'missing')
          AND (
            ? = 0
            OR (
                s.listed_public = 1
                AND st.transcript_status = 'complete'
                AND st.artifact_hash IS NOT NULL
                AND st.artifact_hash = s.reviewed_sha256
                AND s.publication_state = 'reviewed'
                AND s.publication_metadata_sha256
                    = s.reviewed_metadata_sha256
            )
          )
"""


def _fts_tier_candidates(
    conn: sqlite3.Connection,
    *,
    phrase: str,
    column: str,
    candidate_cap: int,
    public_mode: bool,
    limit: int,
) -> list[sqlite3.Row]:
    """Rank one tier inside FTS5 first, then join/filter only the top rows.

    ``rank`` must be read in the same query level as ``MATCH``, and ``ORDER BY
    rank LIMIT`` uses the FTS5 sorter, so a stop-word query never pays a
    b-tree join per matching document. Because the match is column-filtered,
    only one column contributes to ``rank`` and the default per-column
    weights cannot reorder results within the tier.

    Public mode never rank-cuts the mixed corpus: eligible document ids are
    enumerated first and matched exactly (falling back to the deepening loop
    only for very large public corpora). In the loop path, eligibility is
    applied after the rank cut, so the cut alone must not decide membership:
    whenever the filtered candidates cover fewer than ``limit`` distinct
    sessions and matches remain beyond the cap, the cap deepens geometrically
    until the tier is exact.
    """
    match_expr = f"{column} : {phrase}"

    if public_mode:
        # Public eligibility is match-independent and the reviewed-public
        # corpus is orders of magnitude smaller than the private one, so
        # enumerate its document ids from the b-trees and run one EXACT
        # match restricted to them. Rank-cutting the mixed corpus first
        # would let dense private documents decide public membership.
        # Drive from the ~30k session rows, not the ~1.2M document rows:
        # eligibility only reads session-level columns, and the per-session
        # documents index makes the id expansion cheap. An empty public
        # catalog returns immediately.
        eligible_sessions = [
            row[0]
            for row in conn.execute(
                f"""
                SELECT s.session_id
                FROM session_catalog AS s
                JOIN session_search_state AS st
                  ON st.session_id = s.session_id
                WHERE {_ELIGIBILITY_SQL}
                """,
                (SEARCH_INDEX_VERSION, 1),
            )
        ]
        if not eligible_sessions:
            return []
        eligible_ids: list[int] = []
        for start in range(0, len(eligible_sessions), _SQL_IN_CHUNK):
            session_batch = eligible_sessions[start : start + _SQL_IN_CHUNK]
            session_placeholders = ", ".join("?" for _ in session_batch)
            eligible_ids.extend(
                row[0]
                for row in conn.execute(
                    "SELECT id FROM session_search_documents "
                    f"WHERE session_id IN ({session_placeholders})",
                    session_batch,
                )
            )
        if not eligible_ids:
            return []
        if len(eligible_ids) <= _PUBLIC_PUSHDOWN_MAX_DOCS:
            rows: list[sqlite3.Row] = []
            for start in range(0, len(eligible_ids), _SQL_IN_CHUNK):
                batch = eligible_ids[start : start + _SQL_IN_CHUNK]
                placeholders = ", ".join("?" for _ in batch)
                rows.extend(
                    conn.execute(
                        f"""
                        SELECT
                            session_search_fts.rowid AS document_id,
                            rank AS score,
                            d.session_id,
                            d.field_label,
                            s.first_timestamp,
                            s.repo_name,
                            s.project
                        FROM session_search_fts
                        JOIN session_search_documents AS d
                          ON d.id = session_search_fts.rowid
                        JOIN session_catalog AS s
                          ON s.session_id = d.session_id
                        WHERE session_search_fts MATCH ?
                          AND session_search_fts.rowid IN ({placeholders})
                        """,
                        (match_expr, *batch),
                    ).fetchall()
                )
            return rows

    total_matches = conn.execute(
        "SELECT count(*) FROM session_search_fts WHERE session_search_fts MATCH ?",
        (match_expr,),
    ).fetchone()[0]
    cap = max(1, candidate_cap)
    while True:
        # Fetch one candidate beyond the cap from the raw sorter: if it ties
        # the boundary score, the cut splits an equal-rank class and the
        # final newest-first tiebreak could depend on which members happened
        # to make the cut. That only matters when the boundary score can
        # reach the winner set at all — stop-word queries tie at the
        # boundary routinely while their winners sit far above it, so the
        # deepening decision is made after joining, against the limit-th
        # session's best score.
        candidates = conn.execute(
            "SELECT rowid AS document_id, rank AS score "
            "FROM session_search_fts "
            "WHERE session_search_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (match_expr, cap + 1),
        ).fetchall()
        boundary_tie_split = (
            len(candidates) > cap
            and candidates[cap]["score"] == candidates[cap - 1]["score"]
        )
        candidates = candidates[:cap]
        boundary_score = candidates[-1]["score"] if candidates else None
        scores = {row["document_id"]: row["score"] for row in candidates}
        rows: list[sqlite3.Row] = []
        candidate_ids = list(scores)
        for start in range(0, len(candidate_ids), _SQL_IN_CHUNK):
            batch = candidate_ids[start : start + _SQL_IN_CHUNK]
            placeholders = ", ".join("?" for _ in batch)
            rows.extend(
                conn.execute(
                    f"""
                    SELECT
                        d.id AS document_id,
                        d.session_id,
                        d.field_label,
                        s.first_timestamp,
                        s.repo_name,
                        s.project
                    FROM session_search_documents AS d
                    JOIN session_catalog AS s
                      ON s.session_id = d.session_id
                    JOIN session_search_state AS st
                      ON st.session_id = d.session_id
                    WHERE d.id IN ({placeholders})
                      AND {_ELIGIBILITY_SQL}
                    """,
                    (
                        *batch,
                        SEARCH_INDEX_VERSION,
                        1 if public_mode else 0,
                    ),
                ).fetchall()
            )
        scored_rows = [
            {
                **{key: row[key] for key in row.keys()},
                "score": scores[row["document_id"]],
            }
            for row in rows
        ]
        if cap >= total_matches:
            return scored_rows
        session_best: dict[str, float] = {}
        for row in scored_rows:
            best = session_best.get(row["session_id"])
            if best is None or row["score"] < best:
                session_best[row["session_id"]] = row["score"]
        if len(session_best) >= limit:
            # bm25 ascends (lower is better). Documents beyond the cap score
            # no better than the boundary; they can only displace or reorder
            # a winner if the boundary reaches the limit-th session's best.
            worst_winner = sorted(session_best.values())[limit - 1]
            if not boundary_tie_split or (
                boundary_score is not None and boundary_score > worst_winner
            ):
                return scored_rows
        cap = min(total_matches, cap * 8)


def search_sessions(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    public_mode: bool = False,
    candidate_cap: int = TRANSCRIPT_CANDIDATE_CAP,
) -> list[dict[str, Any]]:
    """Search extracted text, returning one best field per visible session.

    The structured tier is ranked ahead of the transcript tier structurally:
    each tier is a separate column-filtered FTS query, so a repeated body
    term can never outrank a goal or summary hit. Within a tier, results are
    ranked by that column's bm25. ``candidate_cap`` bounds how many
    best-scoring documents each tier joins and filters per round; when that
    round covers fewer distinct eligible sessions than ``limit``, the tier
    deepens until it is exact, so the cap is a performance floor and never
    decides membership.
    """
    phrase = _quoted_fts_phrase(query)
    if not phrase or limit <= 0:
        return []
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'session_search_fts'"
        ).fetchone()
        is None
    ):
        raise SearchIndexUnavailable(
            "Extracted-text search index is unavailable; run `logpile sync` first."
        )

    cap = max(1, int(candidate_cap))
    # One read snapshot must cover candidate selection AND snippet fetches:
    # SQLite reuses freed rowids, so across two autocommit statements a
    # winner's rowid could be rebound to different (possibly private) text.
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN")
    try:
        tier_rows = [
            (
                0,
                _fts_tier_candidates(
                    conn,
                    phrase=phrase,
                    column="structured_text",
                    candidate_cap=cap,
                    public_mode=public_mode,
                    limit=limit,
                ),
            ),
            (
                1,
                _fts_tier_candidates(
                    conn,
                    phrase=phrase,
                    column="transcript_text",
                    candidate_cap=cap,
                    public_mode=public_mode,
                    limit=limit,
                ),
            ),
        ]

        best_per_session: dict[str, tuple[tuple[int, float, int], sqlite3.Row]] = {}
        for tier, rows in tier_rows:
            for row in rows:
                order_key = (tier, row["score"], row["document_id"])
                current = best_per_session.get(row["session_id"])
                if current is None or order_key < current[0]:
                    best_per_session[row["session_id"]] = (order_key, row)

        # Stable multi-pass sort: newest first inside equal (tier, score),
        # then session id as the final deterministic tiebreak.
        winners = sorted(best_per_session.items(), key=lambda item: item[0])
        winners.sort(key=lambda item: item[1][1]["first_timestamp"] or "", reverse=True)
        winners.sort(key=lambda item: item[1][0][:2])
        winners = winners[:limit]

        excerpts: dict[int, str] = {}
        winner_ids = [entry[0][2] for _, entry in winners]
        for start in range(0, len(winner_ids), _SQL_IN_CHUNK):
            batch = winner_ids[start : start + _SQL_IN_CHUNK]
            placeholders = ", ".join("?" for _ in batch)
            excerpt_rows = conn.execute(
                f"""
                SELECT
                    session_search_fts.rowid AS document_id,
                    snippet(
                        session_search_fts, -1, '[', ']', ' … ', 28
                    ) AS excerpt
                FROM session_search_fts
                JOIN session_search_documents AS d
                  ON d.id = session_search_fts.rowid
                JOIN session_catalog AS s
                  ON s.session_id = d.session_id
                JOIN session_search_state AS st
                  ON st.session_id = d.session_id
                WHERE session_search_fts MATCH ?
                  AND session_search_fts.rowid IN ({placeholders})
                  AND {_ELIGIBILITY_SQL}
                """,
                (
                    phrase,
                    *batch,
                    SEARCH_INDEX_VERSION,
                    1 if public_mode else 0,
                ),
            ).fetchall()
            for row in excerpt_rows:
                excerpts[row["document_id"]] = row["excerpt"]
    except sqlite3.OperationalError as exc:
        if "session_search" in str(exc).lower() or "fts5" in str(exc).lower():
            raise SearchIndexUnavailable(
                "Extracted-text search index is unavailable; run `logpile sync` first."
            ) from exc
        raise
    finally:
        if own_transaction:
            conn.execute("ROLLBACK")

    results: list[dict[str, Any]] = []
    for session_id, (order_key, row) in winners:
        timestamp = row["first_timestamp"] or ""
        results.append(
            {
                "date": timestamp[:10] or "—",
                "session_id": session_id,
                "matched_field": FIELD_LABELS.get(
                    row["field_label"],
                    row["field_label"].replace("_", " "),
                ),
                "excerpt": (excerpts.get(order_key[2]) or "").strip(),
                "repo_name": row["repo_name"],
                "project": row["project"],
                # bm25 statistics span the whole mixed-visibility corpus, so
                # numeric scores are a side channel on private data; public
                # mode never exposes them.
                "score": None if public_mode else row["score"],
            }
        )
    return results
