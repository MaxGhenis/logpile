"""Extracted-text FTS5 indexing and local session search."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .db import SEARCH_INDEX_VERSION
from .parsers import (
    SearchTranscriptReadError,
    clean_search_text,
    iter_session_search_text,
)


STRUCTURED_FIELDS = (
    ("session_goal", "session_goal", True),
    ("session_summary", "session_summary", False),
    ("first_user_message", "first_user_message", True),
    ("repo_name", "repo_name", False),
    ("project", "project", False),
)
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
        current[column] != _value(row, column)
        for column in _SEARCH_INPUT_COLUMNS
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    complete index revision.
    """
    row = conn.execute(
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
    if row is None:
        remove_session_search_index(conn, session_id)
        return "removed"

    if row["visibility"] == "public":
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
        for field_label, text in _structured_values(row):
            _insert_document(
                conn,
                session_id=session_id,
                field_label=field_label,
                chunk_index=0,
                structured_text=text,
            )

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

        conn.execute(
            """
            INSERT INTO session_search_state (
                session_id, search_version, file_hash, artifact_hash, metadata_hash,
                transcript_status, last_error, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                search_version = excluded.search_version,
                file_hash = excluded.file_hash,
                artifact_hash = excluded.artifact_hash,
                metadata_hash = excluded.metadata_hash,
                transcript_status = excluded.transcript_status,
                last_error = excluded.last_error,
                indexed_at = excluded.indexed_at
            """,
            (
                session_id,
                SEARCH_INDEX_VERSION,
                row["file_hash"],
                artifact_hash,
                search_metadata_hash(row),
                transcript_status,
                (last_error or "")[:1000] or None,
                _now_iso(),
            ),
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


def _record_search_error(
    conn: sqlite3.Connection,
    row: Any,
    *,
    status: str,
    error: str,
) -> None:
    conn.execute(
        """
        INSERT INTO session_search_state (
            session_id, search_version, file_hash, artifact_hash, metadata_hash,
            transcript_status, last_error, indexed_at
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
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


def backfill_search_index(
    conn: sqlite3.Connection,
    *,
    shared_dir: Path | None = None,
    batch_size: int = 50,
    verbose: bool = False,
) -> SearchBackfillStats:
    """Resume the extracted-text index across every durable session row."""
    started = time.monotonic()
    rows = conn.execute(
        """
        SELECT
            s.*,
            COALESCE(u.display_name, s.username) AS display_name,
            u.bio AS bio,
            u.avatar_url AS avatar_url,
            st.search_version AS indexed_search_version,
            st.file_hash AS indexed_file_hash,
            st.artifact_hash AS indexed_artifact_hash,
            st.metadata_hash AS indexed_metadata_hash,
            st.transcript_status AS indexed_transcript_status
        FROM sessions AS s
        LEFT JOIN users AS u ON u.username = s.username
        LEFT JOIN session_search_state AS st
          ON st.session_id = s.session_id
        ORDER BY s.session_id
        """
    ).fetchall()
    scanned = indexed = skipped = missing = errors = indexed_bytes = 0
    attempted_since_commit = 0

    for row in rows:
        scanned += 1
        if _state_is_current(row):
            skipped += 1
            continue

        # Python's sqlite3 does not open a transaction for SAVEPOINT, so an
        # outermost per-session savepoint would autocommit on release. Start
        # the batch transaction explicitly so the periodic commit below is a
        # real 50-session batch boundary.
        if not conn.in_transaction:
            conn.execute("BEGIN")

        if row["visibility"] == "public":
            try:
                replace_session_search_index(
                    conn,
                    row["session_id"],
                    transcript_path=None,
                    shared_dir=shared_dir,
                )
            except (OSError, SearchTranscriptReadError) as exc:
                errors += 1
                _record_search_error(
                    conn,
                    row,
                    status="error",
                    error=str(exc),
                )
            else:
                indexed += 1
                indexed_bytes += max(0, int(row["file_size"] or 0))
        else:
            path = _transcript_path(row)
            if path is None:
                missing += 1
                replace_session_search_index(
                    conn,
                    row["session_id"],
                    transcript_path=None,
                    transcript_status="missing",
                    last_error="No readable source_path or shared_path",
                )
            else:
                try:
                    replace_session_search_index(
                        conn,
                        row["session_id"],
                        transcript_path=path,
                        shared_dir=shared_dir,
                    )
                except (OSError, SearchTranscriptReadError) as exc:
                    errors += 1
                    _record_search_error(
                        conn,
                        row,
                        status="error",
                        error=str(exc),
                    )
                else:
                    indexed += 1
                    indexed_bytes += max(0, int(row["file_size"] or 0))

        attempted_since_commit += 1
        if attempted_since_commit >= max(1, batch_size):
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            attempted_since_commit = 0
        if verbose and (indexed + missing + errors) % 250 == 0:
            print(
                "  Search backfill: "
                f"{indexed + missing + errors}/{scanned} refreshed",
                flush=True,
            )

    # Heal any rows left by an interrupted legacy deletion or a disabled
    # trigger. The documents delete trigger removes the corresponding FTS
    # postings without scanning the virtual table by session id.
    conn.execute(
        """
        DELETE FROM session_search_documents
        WHERE NOT EXISTS (
            SELECT 1 FROM sessions
            WHERE sessions.session_id = session_search_documents.session_id
        )
        """
    )
    conn.execute(
        """
        DELETE FROM session_search_state
        WHERE NOT EXISTS (
            SELECT 1 FROM sessions
            WHERE sessions.session_id = session_search_state.session_id
        )
        """
    )
    if attempted_since_commit:
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    return SearchBackfillStats(
        scanned=scanned,
        indexed=indexed,
        skipped=skipped,
        missing=missing,
        errors=errors,
        indexed_bytes=indexed_bytes,
        elapsed_seconds=time.monotonic() - started,
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
            session_batch = eligible_sessions[start:start + _SQL_IN_CHUNK]
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
                batch = eligible_ids[start:start + _SQL_IN_CHUNK]
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
        "SELECT count(*) FROM session_search_fts "
        "WHERE session_search_fts MATCH ?",
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
            batch = candidate_ids[start:start + _SQL_IN_CHUNK]
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
    if conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'session_search_fts'"
    ).fetchone() is None:
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
            (0, _fts_tier_candidates(
                conn,
                phrase=phrase,
                column="structured_text",
                candidate_cap=cap,
                public_mode=public_mode,
                limit=limit,
            )),
            (1, _fts_tier_candidates(
                conn,
                phrase=phrase,
                column="transcript_text",
                candidate_cap=cap,
                public_mode=public_mode,
                limit=limit,
            )),
        ]

        best_per_session: dict[
            str, tuple[tuple[int, float, int], sqlite3.Row]
        ] = {}
        for tier, rows in tier_rows:
            for row in rows:
                order_key = (tier, row["score"], row["document_id"])
                current = best_per_session.get(row["session_id"])
                if current is None or order_key < current[0]:
                    best_per_session[row["session_id"]] = (order_key, row)

        # Stable multi-pass sort: newest first inside equal (tier, score),
        # then session id as the final deterministic tiebreak.
        winners = sorted(
            best_per_session.items(), key=lambda item: item[0]
        )
        winners.sort(
            key=lambda item: item[1][1]["first_timestamp"] or "", reverse=True
        )
        winners.sort(key=lambda item: item[1][0][:2])
        winners = winners[:limit]

        excerpts: dict[int, str] = {}
        winner_ids = [entry[0][2] for _, entry in winners]
        for start in range(0, len(winner_ids), _SQL_IN_CHUNK):
            batch = winner_ids[start:start + _SQL_IN_CHUNK]
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
