"""Derived-row writers, migrations and native refresh write only what changed.

The oracle for every diff writer is its pre-diff delete-and-reinsert version
(tests/legacy_db_writers.py): starting from the rows a previous write left,
the diff writer must end with exactly the rows the old writer would leave for
the new input, and repeating an identical call must change nothing at all.
"""

import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from legacy_db_writers import (
    legacy_apply_message_claims,
    legacy_insert_session_daily_usage,
    legacy_insert_session_paths,
    legacy_insert_tool_calls,
)

from logpile import db as logpile_db
from logpile.db import (
    CLAIMS_TOKEN_VERSION,
    DATA_REPAIR_VERSION,
    NATIVE_TOKEN_COLUMNS,
    apply_message_claims,
    delete_transcript_checkpoint,
    drain_native_refresh,
    get_db,
    get_meta,
    get_transcript_checkpoint,
    init_db,
    insert_session_daily_usage,
    insert_session_paths,
    insert_tool_calls,
    iter_transcript_checkpoints,
    migrate_db,
    put_transcript_checkpoint,
    queue_native_refresh,
    refresh_native_usage,
    set_meta,
)
from logpile.parsers import (
    DailyUsage,
    MessageUsage,
    ParseCheckpoint,
    SessionPath,
    ToolCall,
)
from logpile.sync import sync_sessions

PROPERTY_SETTINGS = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

_TEMPLATE: sqlite3.Connection | None = None


def fresh_db() -> sqlite3.Connection:
    """An in-memory, fully migrated database (copied from one template)."""
    global _TEMPLATE
    if _TEMPLATE is None:
        _TEMPLATE = sqlite3.connect(":memory:")
        _TEMPLATE.row_factory = sqlite3.Row
        migrate_db(_TEMPLATE)
        _TEMPLATE.commit()
    conn = sqlite3.connect(":memory:")
    _TEMPLATE.backup(conn)
    conn.row_factory = sqlite3.Row
    return conn


def clone_db(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.commit()
    copy = sqlite3.connect(":memory:")
    conn.backup(copy)
    copy.row_factory = sqlite3.Row
    return copy


def add_session(
    conn,
    session_id: str,
    *,
    source: str = "claudecode",
    first_timestamp: str | None = "2026-09-28T10:00:00Z",
    last_timestamp: str | None = "2026-09-28T11:00:00Z",
    token_version: int = CLAIMS_TOKEN_VERSION,
) -> None:
    conn.execute(
        """
        INSERT INTO sessions (
            session_id, source, username, source_path, shared_path,
            first_timestamp, last_timestamp, token_version
        ) VALUES (?, ?, 'alice', ?, '', ?, ?, ?)
        """,
        (
            session_id,
            source,
            f"/tmp/{session_id}.jsonl",
            first_timestamp,
            last_timestamp,
            token_version,
        ),
    )


def rows(conn, sql: str, params=()) -> list[tuple]:
    return [tuple(row) for row in conn.execute(sql, params)]


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record))
            fh.write("\n")


class WriteCounter:
    """Counts row writes to main-database tables through TEMP triggers.

    conn.total_changes also counts writes to temp tables, which some writers
    use for staging; these triggers see only the named persistent tables.
    """

    def __init__(self, conn, tables: tuple[str, ...]) -> None:
        self.conn = conn
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _test_writes (tbl TEXT, op TEXT)")
        for table in tables:
            for op in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(
                    f"""
                    CREATE TEMP TRIGGER IF NOT EXISTS _test_{table}_{op.lower()}
                    AFTER {op} ON main.{table} BEGIN
                        INSERT INTO _test_writes VALUES ('{table}', '{op}');
                    END
                    """
                )

    def reset(self) -> None:
        self.conn.execute("DELETE FROM _test_writes")

    def counts(self) -> dict[tuple[str, str], int]:
        return {
            (table, op): count
            for table, op, count in self.conn.execute(
                "SELECT tbl, op, COUNT(*) FROM _test_writes GROUP BY tbl, op"
            )
        }


# ── tool_calls ────────────────────────────────────────────────────────────

tool_call = st.builds(
    ToolCall,
    tool_name=st.sampled_from(["Bash", "Read", "Edit"]),
    command=st.sampled_from([None, "", "ls", "pytest -q"]),
    timestamp=st.sampled_from([None, "2026-09-29T10:00:00Z", "2026-09-29T11:00:00.5Z"]),
    is_error=st.booleans(),
)
tool_call_lists = st.lists(tool_call, max_size=10)


@st.composite
def before_after_tool_calls(draw):
    before = draw(tool_call_lists)
    kind = draw(st.sampled_from(["independent", "append", "edit"]))
    if kind == "independent":
        after = draw(tool_call_lists)
    elif kind == "append":
        after = before + draw(tool_call_lists)
    else:
        after = list(before)
        for index in draw(st.lists(st.integers(0, max(len(after) - 1, 0)))):
            if after:
                after[index] = draw(tool_call)
        after = after[: draw(st.integers(0, len(after)))] + draw(tool_call_lists)
    return before, after


def tool_call_rows(conn, session_id: str) -> list[tuple]:
    return rows(
        conn,
        "SELECT id, tool_name, command, timestamp, is_error FROM tool_calls "
        "WHERE session_id = ? ORDER BY id",
        (session_id,),
    )


class ToolCallWriterTests(unittest.TestCase):
    def _seed(self, before: list[ToolCall]) -> sqlite3.Connection:
        conn = fresh_db()
        # Interleave a neighbour's rows so the session's ids are not
        # contiguous and appended ids land after the neighbour's.
        legacy_insert_tool_calls(conn, "target", before[: len(before) // 2])
        legacy_insert_tool_calls(conn, "neighbour", before[:3])
        legacy_insert_tool_calls(conn, "target", before)
        return conn

    @PROPERTY_SETTINGS
    @given(case=before_after_tool_calls())
    def test_diff_equals_delete_and_reinsert_and_repeat_writes_nothing(self, case):
        before, after = case
        conn = self._seed(before)
        reference = clone_db(conn)
        stored_before = tool_call_rows(conn, "target")
        neighbour_before = tool_call_rows(conn, "neighbour")

        insert_tool_calls(conn, "target", (call for call in after))
        legacy_insert_tool_calls(reference, "target", after)

        subject = tool_call_rows(conn, "target")
        self.assertEqual(
            [row[1:] for row in subject],
            [row[1:] for row in tool_call_rows(reference, "target")],
        )
        # Positions both lists share keep their row, changed or not.
        shared = min(len(before), len(after))
        self.assertEqual(
            [row[0] for row in subject[:shared]],
            [row[0] for row in stored_before[:shared]],
        )
        self.assertEqual(tool_call_rows(conn, "neighbour"), neighbour_before)

        changes = conn.total_changes
        insert_tool_calls(conn, "target", after)
        self.assertEqual(conn.total_changes, changes)
        self.assertEqual(tool_call_rows(conn, "target"), subject)

    def test_pages_boundaries_shrink_and_growth(self) -> None:
        page = logpile_db._TOOL_CALL_PAGE_ROWS
        before = [
            ToolCall(tool_name="Bash", command=f"echo {index}", is_error=False)
            for index in range(2 * page + 345)
        ]
        edited = list(before)
        for index in (page - 1, page, page + 1, 2 * page):
            edited[index] = ToolCall(tool_name="Read", command=f"cat {index}")
        for after in (
            edited + [ToolCall(tool_name="Edit")] * 7,
            edited[: page + 500],
            edited[:page],
            [],
        ):
            with self.subTest(length=len(after)):
                conn = self._seed(before)
                reference = clone_db(conn)
                ids_before = [row[0] for row in tool_call_rows(conn, "target")]
                insert_tool_calls(conn, "target", iter(after))
                legacy_insert_tool_calls(reference, "target", after)
                subject = tool_call_rows(conn, "target")
                self.assertEqual(
                    [row[1:] for row in subject],
                    [row[1:] for row in tool_call_rows(reference, "target")],
                )
                shared = min(len(before), len(after))
                self.assertEqual(
                    [row[0] for row in subject[:shared]], ids_before[:shared]
                )

    def test_changed_row_updates_only_differing_columns(self) -> None:
        conn = fresh_db()
        legacy_insert_tool_calls(
            conn, "target", [ToolCall(tool_name="Bash", command="pytest")]
        )
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        insert_tool_calls(
            conn,
            "target",
            [ToolCall(tool_name="Bash", command="pytest", is_error=True)],
        )
        conn.set_trace_callback(None)
        updates = [sql for sql in statements if sql.startswith("UPDATE tool_calls")]
        self.assertEqual(len(updates), 1)
        self.assertIn("SET is_error = 1", updates[0])
        self.assertNotIn("tool_name", updates[0])


# ── session_paths ─────────────────────────────────────────────────────────

session_path = st.builds(
    SessionPath,
    raw_path=st.sampled_from(["a.py", "./a.py", "/w/a.py"]),
    normalized_path=st.sampled_from(["/w/a.py", "/w/b.py", "/w/é.py"]),
    display_path=st.sampled_from(["a.py", "w/a.py"]),
    relative_path=st.sampled_from([None, "a.py"]),
    operation=st.sampled_from(["read", "write"]),
    source=st.sampled_from(["tool_input", "command"]),
    repo_relative_path=st.sampled_from([None, "src/a.py"]),
    tool_name=st.sampled_from([None, "", "Read"]),
    timestamp=st.sampled_from(
        [None, "2026-09-29T09:00:00Z", "2026-09-29T10:00:00Z", "2026-09-30T00:00:00Z"]
    ),
)
session_path_lists = st.lists(session_path, max_size=12)


@st.composite
def before_after_paths(draw):
    before = draw(session_path_lists)
    kind = draw(st.sampled_from(["independent", "append", "truncate"]))
    if kind == "independent":
        after = draw(session_path_lists)
    elif kind == "append":
        after = before + draw(session_path_lists)
    else:
        after = before[: draw(st.integers(0, len(before)))]
    return before, after


PATH_COLUMNS = (
    "session_id, raw_path, normalized_path, relative_path, repo_relative_path, "
    "display_path, operation, source, tool_name, first_timestamp, "
    "last_timestamp, occurrence_count"
)


def path_rows(conn, session_id: str) -> list[tuple]:
    return sorted(
        rows(
            conn,
            f"SELECT {PATH_COLUMNS} FROM session_paths WHERE session_id = ?",
            (session_id,),
        ),
        key=repr,
    )


def path_ids_by_value(conn, session_id: str) -> dict[tuple, int]:
    return {
        tuple(row[1:]): row[0]
        for row in conn.execute(
            f"SELECT id, {PATH_COLUMNS} FROM session_paths WHERE session_id = ?",
            (session_id,),
        )
    }


class SessionPathWriterTests(unittest.TestCase):
    @PROPERTY_SETTINGS
    @given(case=before_after_paths(), neighbour=session_path_lists)
    def test_diff_equals_delete_and_reinsert_and_repeat_writes_nothing(
        self, case, neighbour
    ):
        before, after = case
        conn = fresh_db()
        legacy_insert_session_paths(conn, "target", before)
        legacy_insert_session_paths(conn, "neighbour", neighbour)
        reference = clone_db(conn)
        ids_before = path_ids_by_value(conn, "target")
        neighbour_before = path_ids_by_value(conn, "neighbour")

        insert_session_paths(conn, "target", (path for path in after))
        legacy_insert_session_paths(reference, "target", after)

        self.assertEqual(path_rows(conn, "target"), path_rows(reference, "target"))
        # A row whose aggregate did not change keeps its id.
        for values, row_id in path_ids_by_value(conn, "target").items():
            if values in ids_before:
                self.assertEqual(row_id, ids_before[values])
        self.assertEqual(path_ids_by_value(conn, "neighbour"), neighbour_before)

        changes = conn.total_changes
        insert_session_paths(conn, "target", after)
        self.assertEqual(conn.total_changes, changes)

    def test_duplicate_stored_keys_collapse_to_one_row(self) -> None:
        conn = fresh_db()
        path = SessionPath(
            raw_path="a.py",
            normalized_path="/w/a.py",
            display_path="a.py",
            relative_path="a.py",
            operation="read",
            source="tool_input",
            tool_name="Read",
            timestamp="2026-09-29T10:00:00Z",
        )
        legacy_insert_session_paths(conn, "target", [path])
        # A duplicate of the key that no current writer produces.
        conn.execute(
            f"INSERT INTO session_paths ({PATH_COLUMNS}) "
            f"SELECT {PATH_COLUMNS} FROM session_paths WHERE session_id = 'target'"
        )
        kept_id = conn.execute(
            "SELECT MIN(id) FROM session_paths WHERE session_id = 'target'"
        ).fetchone()[0]
        reference = fresh_db()
        legacy_insert_session_paths(reference, "target", [path])

        insert_session_paths(conn, "target", [path])

        self.assertEqual(path_rows(conn, "target"), path_rows(reference, "target"))
        self.assertEqual(
            rows(conn, "SELECT id FROM session_paths WHERE session_id = 'target'"),
            [(kept_id,)],
        )

    def test_staging_leaves_no_temp_tables_on_the_connection(self) -> None:
        conn = fresh_db()
        insert_session_paths(
            conn,
            "target",
            [
                SessionPath(
                    raw_path="a.py",
                    normalized_path="/w/a.py",
                    display_path="a.py",
                    relative_path=None,
                    operation="read",
                    source="tool_input",
                )
            ],
        )
        self.assertEqual(
            rows(conn, "SELECT name FROM temp.sqlite_master WHERE type = 'table'"),
            [],
        )


# ── session_daily_usage ───────────────────────────────────────────────────

DAYS = ["2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30"]
DAILY_COMPONENTS = (
    "total_input_tokens",
    "total_output_tokens",
    "fresh_input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
    "cache_creation_unknown_input_tokens",
    "reasoning_output_tokens",
    "user_message_count",
    "assistant_message_count",
    "tool_call_count",
)
NATIVE_COLUMNS = tuple(native for native, _ in NATIVE_TOKEN_COLUMNS)


@st.composite
def daily_slices(draw):
    days = draw(st.lists(st.sampled_from(DAYS), unique=True, max_size=4))
    small = st.integers(0, 2)
    slices = []
    for day in days:
        split = [draw(small), draw(small), draw(small)]
        slices.append(
            DailyUsage(
                day=day,
                total_input_tokens=draw(small),
                total_output_tokens=draw(small),
                fresh_input_tokens=draw(small),
                cached_input_tokens=draw(small),
                cache_creation_input_tokens=sum(split),
                cache_creation_5m_input_tokens=split[0],
                cache_creation_1h_input_tokens=split[1],
                cache_creation_unknown_input_tokens=split[2],
                reasoning_output_tokens=draw(small),
                user_message_count=draw(small),
                assistant_message_count=draw(small),
                tool_call_count=draw(small),
                approximated=draw(st.booleans()),
            )
        )
    return slices


def reconcile_session(conn, session_id: str, slices: list[DailyUsage]) -> None:
    """Set the session totals the daily slices must sum to (as upsert does)."""
    assignments = ", ".join(f"{column} = ?" for column in DAILY_COMPONENTS)
    conn.execute(
        f"UPDATE sessions SET {assignments} WHERE session_id = ?",
        (
            *(
                sum(getattr(day, column) for day in slices)
                for column in DAILY_COMPONENTS
            ),
            session_id,
        ),
    )


def daily_rows(conn, session_id: str, *, natives: bool) -> list[tuple]:
    columns = ["day", *DAILY_COMPONENTS, "approximated"]
    if natives:
        columns += NATIVE_COLUMNS
    return rows(
        conn,
        f"SELECT {', '.join(columns)} FROM session_daily_usage "
        "WHERE session_id = ? ORDER BY day",
        (session_id,),
    )


class DailyUsageWriterTests(unittest.TestCase):
    @PROPERTY_SETTINGS
    @given(
        before=daily_slices(),
        after=daily_slices(),
        source=st.sampled_from(["claudecode", "codex"]),
    )
    def test_diff_equals_delete_and_reinsert_and_repeat_writes_nothing(
        self, before, after, source
    ):
        conn = fresh_db()
        add_session(conn, "target", source=source)
        reconcile_session(conn, "target", before)
        legacy_insert_session_daily_usage(conn, "target", before)
        # Sentinel natives show which rows the writer touched.
        conn.execute(
            f"UPDATE session_daily_usage SET "
            f"{', '.join(f'{column} = 777' for column in NATIVE_COLUMNS)}"
        )
        reconcile_session(conn, "target", after)
        reference = clone_db(conn)
        natives_before = {
            row[0]: row[-len(NATIVE_COLUMNS) :]
            for row in daily_rows(conn, "target", natives=True)
        }

        insert_session_daily_usage(conn, "target", after)
        legacy_insert_session_daily_usage(reference, "target", after)

        self.assertEqual(
            daily_rows(conn, "target", natives=False),
            daily_rows(reference, "target", natives=False),
        )
        for row in daily_rows(conn, "target", natives=True):
            day, natives = row[0], row[-len(NATIVE_COLUMNS) :]
            # Surviving days keep native_* for refresh_native_usage to own.
            self.assertEqual(
                natives,
                natives_before.get(day, (0,) * len(NATIVE_COLUMNS)),
            )

        changes = conn.total_changes
        insert_session_daily_usage(conn, "target", after)
        self.assertEqual(conn.total_changes, changes)

        # After the refresh sync always runs, every column matches.
        refresh_native_usage(conn, {"target"})
        refresh_native_usage(reference, {"target"})
        self.assertEqual(
            daily_rows(conn, "target", natives=True),
            daily_rows(reference, "target", natives=True),
        )

    def test_validation_still_rejects_before_writing(self) -> None:
        good = DailyUsage(day="2026-09-29", total_input_tokens=5)
        conn = fresh_db()
        add_session(conn, "target")
        reconcile_session(conn, "target", [good])
        insert_session_daily_usage(conn, "target", [good])
        stored = daily_rows(conn, "target", natives=True)
        with self.assertRaisesRegex(ValueError, "does not reconcile"):
            insert_session_daily_usage(
                conn, "target", [DailyUsage(day="2026-09-29", total_input_tokens=6)]
            )
        bad_split = DailyUsage(
            day="2026-09-29",
            total_input_tokens=5,
            cache_creation_input_tokens=0,
            cache_creation_5m_input_tokens=1,
        )
        conn.execute(
            "UPDATE sessions SET cache_creation_5m_input_tokens = 1 "
            "WHERE session_id = 'target'"
        )
        with self.assertRaisesRegex(ValueError, "split does not reconcile"):
            insert_session_daily_usage(conn, "target", [bad_split])
        self.assertEqual(daily_rows(conn, "target", natives=True), stored)

    def test_duplicate_days_raise_the_same_integrity_error(self) -> None:
        slices = [
            DailyUsage(day="2026-09-29", total_input_tokens=1),
            DailyUsage(day="2026-09-29", total_input_tokens=2),
        ]
        for writer in (insert_session_daily_usage, legacy_insert_session_daily_usage):
            with self.subTest(writer=writer.__name__):
                conn = fresh_db()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE"):
                    writer(conn, "target", slices)


# ── message_claims ────────────────────────────────────────────────────────

CLAIM_KEYS = ["k1", "k2", "k3", "k4", "k5"]
CLAIM_DAYS = [None, "2026-09-28", "2026-09-29"]
TIMESTAMPS = [
    None,
    "2026-09-28T10:00:00Z",
    "2026-09-29T10:00:00Z",
    "2026-09-30T10:00:00Z",
]
SESSIONS = ["s1", "s2", "s3"]

message = st.builds(
    lambda key, day, fresh, out, total, c5, c1, cu: MessageUsage(
        claim_key=key,
        day=day,
        model="claude-test",
        fresh_input_tokens=fresh,
        cached_input_tokens=0,
        cache_creation_input_tokens=total,
        cache_creation_5m_input_tokens=c5,
        cache_creation_1h_input_tokens=c1,
        cache_creation_unknown_input_tokens=cu,
        output_tokens=out,
    ),
    key=st.sampled_from(CLAIM_KEYS),
    day=st.sampled_from(CLAIM_DAYS),
    fresh=st.integers(0, 3),
    out=st.integers(0, 3),
    total=st.integers(0, 3),
    c5=st.integers(0, 2),
    c1=st.integers(0, 2),
    cu=st.integers(0, 2),
)
claim_lists = st.lists(message, max_size=6)
spans = st.tuples(st.sampled_from(TIMESTAMPS), st.sampled_from(TIMESTAMPS))


def claims_ledger(conn) -> list[tuple]:
    return rows(conn, "SELECT * FROM message_claims ORDER BY claim_key, session_id")


def natives_snapshot(conn) -> list[tuple]:
    columns = ", ".join(NATIVE_COLUMNS)
    return rows(
        conn, f"SELECT session_id, {columns} FROM sessions ORDER BY session_id"
    ) + rows(
        conn,
        f"SELECT session_id, day, {columns} FROM session_daily_usage "
        "ORDER BY session_id, day",
    )


def claims_world(session_spans, session_claims) -> sqlite3.Connection:
    conn = fresh_db()
    for session_id, (first, last) in zip(SESSIONS, session_spans, strict=True):
        add_session(conn, session_id, first_timestamp=first, last_timestamp=last)
        for day in CLAIM_DAYS[1:]:
            conn.execute(
                "INSERT INTO session_daily_usage (session_id, day) VALUES (?, ?)",
                (session_id, day),
            )
    for session_id, claims in zip(SESSIONS, session_claims, strict=True):
        legacy_apply_message_claims(conn, session_id, claims)
    refresh_native_usage(conn, None)
    return conn


class MessageClaimsTests(unittest.TestCase):
    @PROPERTY_SETTINGS
    @given(
        session_spans=st.lists(spans, min_size=3, max_size=3),
        session_claims=st.lists(claim_lists, min_size=3, max_size=3),
        target=st.sampled_from(SESSIONS),
        new_span=spans,
        new_claims=claim_lists,
    )
    def test_matches_legacy_and_scoped_refresh_equals_full_refresh(
        self, session_spans, session_claims, target, new_span, new_claims
    ):
        conn = claims_world(session_spans, session_claims)
        reference = clone_db(conn)
        for database in (conn, reference):
            # Sync upserts the session row (and so its rank) first.
            database.execute(
                "UPDATE sessions SET first_timestamp = ?, last_timestamp = ? "
                "WHERE session_id = ?",
                (*new_span, target),
            )

        affected = apply_message_claims(conn, target, new_claims)
        legacy_affected = legacy_apply_message_claims(reference, target, new_claims)

        self.assertEqual(affected, legacy_affected)
        self.assertEqual(claims_ledger(conn), claims_ledger(reference))

        refresh_native_usage(conn, affected)
        scoped = natives_snapshot(conn)
        refresh_native_usage(conn, None)
        self.assertEqual(scoped, natives_snapshot(conn))

        counter = WriteCounter(conn, ("message_claims",))
        self.assertEqual(
            apply_message_claims(conn, target, new_claims),
            legacy_apply_message_claims(reference, target, new_claims),
        )
        self.assertEqual(counter.counts(), {})

    def test_changed_claim_is_updated_and_unchanged_claims_are_not_written(self):
        first = MessageUsage(
            claim_key="k1", day="2026-09-29", model="m", output_tokens=1
        )
        second = MessageUsage(
            claim_key="k2", day="2026-09-29", model="m", output_tokens=2
        )
        conn = fresh_db()
        add_session(conn, "s1")
        apply_message_claims(conn, "s1", [first, second])
        counter = WriteCounter(conn, ("message_claims",))
        apply_message_claims(
            conn,
            "s1",
            [
                first,
                MessageUsage(
                    claim_key="k2", day="2026-09-29", model="m", output_tokens=3
                ),
            ],
        )
        self.assertEqual(counter.counts(), {("message_claims", "UPDATE"): 1})

    def test_no_statement_scans_the_claims_table(self) -> None:
        conn = fresh_db()
        for index in range(40):
            add_session(conn, f"s{index}")
            legacy_apply_message_claims(
                conn,
                f"s{index}",
                [
                    MessageUsage(claim_key=f"k{index + offset}", day=None, model=None)
                    for offset in range(5)
                ],
            )
        for analyzed in (False, True):
            with self.subTest(analyzed=analyzed):
                if analyzed:
                    conn.execute("ANALYZE")
                statements: list[str] = []
                conn.set_trace_callback(statements.append)
                apply_message_claims(
                    conn,
                    "s7",
                    [MessageUsage(claim_key="k9", day=None, model=None)],
                )
                conn.set_trace_callback(None)
                checked = 0
                for sql in statements:
                    if "message_claims " not in sql and "message_claims\n" not in sql:
                        continue
                    if sql.lstrip().upper().startswith(("CREATE", "BEGIN")):
                        continue
                    plan = [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}")]
                    checked += 1
                    for detail in plan:
                        self.assertFalse(
                            detail.startswith(("SCAN claims", "SCAN message_claims")),
                            f"{detail!r} in plan of {sql}",
                        )
                self.assertGreaterEqual(checked, 4)
        plan = [
            row[3]
            for row in conn.execute(
                f"EXPLAIN QUERY PLAN {logpile_db._TOUCHED_CLAIMANTS_SQL}"
            )
        ]
        self.assertFalse(any(detail.startswith("SCAN claims") for detail in plan))
        self.assertIn(
            "SEARCH claims USING PRIMARY KEY (claim_key=?)", plan, f"plan: {plan}"
        )


# ── native refresh queue ──────────────────────────────────────────────────


def queue_contents(conn) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT session_id FROM native_refresh_queue ORDER BY session_id"
        )
    ]


def corrupt_natives(conn, session_ids) -> None:
    for session_id in session_ids:
        conn.execute(
            "UPDATE sessions SET native_total_input_tokens = -1, "
            "native_assistant_message_count = -1 WHERE session_id = ?",
            (session_id,),
        )
        conn.execute(
            "UPDATE session_daily_usage SET native_total_output_tokens = -1 "
            "WHERE session_id = ?",
            (session_id,),
        )


class NativeRefreshQueueTests(unittest.TestCase):
    def _world(self) -> sqlite3.Connection:
        claims = [
            [
                MessageUsage(
                    claim_key="k1", day="2026-09-28", model="m", output_tokens=5
                )
            ],
            [
                MessageUsage(
                    claim_key="k1", day="2026-09-28", model="m", output_tokens=5
                ),
                MessageUsage(
                    claim_key="k2", day="2026-09-29", model="m", output_tokens=7
                ),
            ],
            [
                MessageUsage(
                    claim_key="k3", day="2026-09-29", model="m", output_tokens=9
                )
            ],
        ]
        spans = [
            ("2026-09-28T10:00:00Z", "2026-09-28T11:00:00Z"),
            ("2026-09-28T10:00:00Z", "2026-09-29T11:00:00Z"),
            (None, None),
        ]
        conn = claims_world(spans, claims)
        add_session(conn, "codex-1", source="codex")
        conn.execute(
            "UPDATE sessions SET total_input_tokens = 40 WHERE session_id = 'codex-1'"
        )
        refresh_native_usage(conn, None)
        return conn

    def test_drain_refreshes_queued_sessions_and_empties_the_queue(self) -> None:
        conn = self._world()
        expected = natives_snapshot(conn)
        corrupt_natives(conn, ["s1", "s3", "codex-1"])
        queue_native_refresh(conn, ["s1", "s3", "codex-1", "s3"])
        self.assertEqual(queue_contents(conn), ["codex-1", "s1", "s3"])

        self.assertEqual(drain_native_refresh(conn, chunk_size=2), 0)

        self.assertEqual(natives_snapshot(conn), expected)
        self.assertEqual(queue_contents(conn), [])

    @PROPERTY_SETTINGS
    @given(
        queued=st.lists(st.sampled_from([*SESSIONS, "codex-1", "gone"]), unique=True),
        chunk_size=st.integers(1, 4),
        chunks_per_drain=st.integers(0, 3),
    )
    def test_bounded_drains_converge_to_a_full_refresh(
        self, queued, chunk_size, chunks_per_drain
    ):
        conn = self._world()
        expected = natives_snapshot(conn)
        corrupt_natives(conn, [session for session in queued if session != "gone"])
        queue_native_refresh(conn, queued)
        remaining = len(queued)
        for _ in range(len(queued) + 1):
            # Each monotonic() read advances one tick: the deadline admits
            # exactly chunks_per_drain chunks per drain.
            ticks = iter(range(10_000))
            with mock.patch.object(
                logpile_db.time,
                "monotonic",
                side_effect=lambda ticks=ticks: next(ticks),
            ):
                left = drain_native_refresh(
                    conn, deadline=chunks_per_drain, chunk_size=chunk_size
                )
            self.assertEqual(left, max(0, remaining - chunk_size * chunks_per_drain))
            remaining = left
            if chunks_per_drain == 0:
                break
        if chunks_per_drain:
            self.assertEqual(remaining, 0)
            self.assertEqual(natives_snapshot(conn), expected)
        else:
            self.assertEqual(queue_contents(conn), sorted(queued))

    def test_each_chunk_is_committed_before_the_deadline_stops_the_drain(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            with get_db(db_path) as conn:
                for index in range(5):
                    add_session(conn, f"s{index}")
                queue_native_refresh(conn, [f"s{index}" for index in range(5)])
            with get_db(db_path) as conn:
                ticks = iter([0.0, 0.5, 1.0])
                with mock.patch.object(
                    logpile_db.time, "monotonic", side_effect=lambda: next(ticks)
                ):
                    left = drain_native_refresh(conn, deadline=1.0, chunk_size=2)
                self.assertEqual(left, 1)
                # A separate connection sees the drained chunks: committed.
                with closing(sqlite3.connect(db_path)) as other:
                    self.assertEqual(queue_contents(other), ["s4"])
                conn.rollback()
                self.assertEqual(queue_contents(conn), ["s4"])

    def test_past_deadline_drains_nothing(self) -> None:
        conn = self._world()
        queue_native_refresh(conn, ["s1"])
        self.assertEqual(drain_native_refresh(conn, deadline=time.monotonic() - 1), 1)

    def test_legacy_pending_flag_is_adopted_once(self) -> None:
        conn = self._world()
        expected = natives_snapshot(conn)
        corrupt_natives(conn, ["s2", "codex-1"])
        set_meta(conn, "native_refresh_pending", "1")
        with mock.patch.object(logpile_db.time, "monotonic", return_value=5.0):
            self.assertEqual(drain_native_refresh(conn, deadline=0.0), 4)
        self.assertEqual(get_meta(conn, "native_refresh_pending"), "0")
        self.assertEqual(queue_contents(conn), ["codex-1", "s1", "s2", "s3"])
        self.assertEqual(drain_native_refresh(conn), 0)
        self.assertEqual(natives_snapshot(conn), expected)

    def test_flag_left_at_zero_or_absent_asks_for_nothing(self) -> None:
        for flag in ("0", None):
            with self.subTest(flag=flag):
                conn = self._world()
                set_meta(conn, "native_refresh_pending", flag)
                corrupt_natives(conn, ["s1"])
                stale = natives_snapshot(conn)
                self.assertEqual(drain_native_refresh(conn), 0)
                self.assertEqual(natives_snapshot(conn), stale)
                self.assertEqual(get_meta(conn, "native_refresh_pending"), flag)

    def test_refresh_native_usage_dequeues_what_it_refreshes(self) -> None:
        conn = self._world()
        queue_native_refresh(conn, ["s1", "s2", "s3"])
        refresh_native_usage(conn, {"s2"})
        self.assertEqual(queue_contents(conn), ["s1", "s3"])
        refresh_native_usage(conn, None)
        self.assertEqual(queue_contents(conn), [])

    def test_queue_rejects_a_bare_string(self) -> None:
        conn = fresh_db()
        with self.assertRaises(TypeError):
            queue_native_refresh(conn, "s1")

    def test_deleting_a_session_drops_its_claims_and_queues_other_claimants(self):
        conn = self._world()
        queue_native_refresh(conn, ["s1"])
        put_transcript_checkpoint(
            conn,
            source_path="/tmp/s1.jsonl",
            session_id="s1",
            source="claudecode",
            checkpoint=ParseCheckpoint(10, "a" * 64, "gen", 1, 2),
            state_file="/tmp/s1.state",
            file_mtime=1.0,
            now="2026-09-29T00:00:00Z",
        )

        conn.execute("DELETE FROM sessions WHERE session_id = 's1'")

        self.assertEqual(
            rows(conn, "SELECT COUNT(*) FROM message_claims WHERE session_id = 's1'"),
            [(0,)],
        )
        # s2 shared k1 with s1; s3 shared nothing and is not queued.
        self.assertEqual(queue_contents(conn), ["s2"])
        self.assertIsNone(get_transcript_checkpoint(conn, "/tmp/s1.jsonl"))
        self.assertEqual(drain_native_refresh(conn), 0)
        drained = natives_snapshot(conn)
        refresh_native_usage(conn, None)
        self.assertEqual(drained, natives_snapshot(conn))
        owner = conn.execute(
            "SELECT owner_session_id FROM message_claim_owners WHERE claim_key = 'k1'"
        ).fetchone()[0]
        self.assertEqual(owner, "s2")


# ── migrate_db ────────────────────────────────────────────────────────────


def _claude_records(session_id: str, *, extra: int = 0) -> list[dict]:
    records = [
        {
            "timestamp": "2026-09-28T10:00:00Z",
            "type": "user",
            "cwd": "/tmp/demo",
            "sessionId": session_id,
            "message": {"content": "ship the fix"},
        },
        {
            "timestamp": "2026-09-28T10:00:05Z",
            "type": "assistant",
            "requestId": "req-1",
            "uuid": "uuid-1",
            "message": {
                "id": "msg-1",
                "model": "claude-test",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "id": "read-1",
                        "input": {"file_path": "/tmp/demo/src/app.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": "test-1",
                        "input": {"command": "pytest -q"},
                    },
                ],
            },
        },
        {
            "timestamp": "2026-09-28T10:00:06Z",
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "test-1", "content": "ok"}
                ]
            },
        },
    ]
    for index in range(extra):
        records.append(
            {
                "timestamp": f"2026-09-29T1{index % 10}:00:00Z",
                "type": "assistant",
                "requestId": f"req-x{index}",
                "uuid": f"uuid-x{index}",
                "message": {
                    "id": f"msg-x{index}",
                    "model": "claude-test",
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "id": f"edit-{index}",
                            "input": {"file_path": f"/tmp/demo/src/new_{index}.py"},
                        }
                    ],
                },
            }
        )
    return records


def _codex_records() -> list[dict]:
    return [
        {
            "timestamp": "2026-09-28T09:00:00Z",
            "type": "session_meta",
            "payload": {"id": "codex-thread", "cwd": "/tmp/demo"},
        },
        {
            "timestamp": "2026-09-28T09:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        },
        {
            "timestamp": "2026-09-28T09:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 50,
                        "cached_input_tokens": 10,
                        "output_tokens": 5,
                    }
                },
            },
        },
    ]


def populated_home(root: Path) -> tuple[Path, Path, Path]:
    home = root / "home"
    shared = root / "shared"
    db_path = root / "logpile.db"
    project = home / ".claude" / "projects" / "-tmp-demo"
    write_jsonl(project / "session-a.jsonl", _claude_records("session-a"))
    write_jsonl(project / "session-b.jsonl", _claude_records("session-b", extra=3))
    write_jsonl(
        home / ".codex" / "sessions" / "2026" / "09" / "28" / "rollout-codex-1.jsonl",
        _codex_records(),
    )
    return home, shared, db_path


def wal_bytes(db_path: Path) -> int:
    wal = Path(f"{db_path}-wal")
    return wal.stat().st_size if wal.exists() else 0


class MigrateWritesNothingTests(unittest.TestCase):
    def _open(self, db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _assert_second_migration_is_read_only(self, db_path: Path) -> None:
        with closing(self._open(db_path)) as conn:
            migrate_db(conn)
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            changes = conn.total_changes
            schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
            self.assertEqual(wal_bytes(db_path), 0)

            migrate_db(conn)
            conn.commit()

            self.assertEqual(conn.total_changes, changes)
            self.assertEqual(wal_bytes(db_path), 0)
            self.assertEqual(
                conn.execute("PRAGMA schema_version").fetchone()[0], schema_version
            )

    def test_repeat_migration_of_a_synced_database_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home, shared, db_path = populated_home(Path(td))
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with closing(self._open(db_path)) as conn:
                counts = {
                    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "sessions",
                        "message_claims",
                        "session_daily_usage",
                        "session_paths",
                        "tool_calls",
                    )
                }
            for table, count in counts.items():
                self.assertGreater(count, 0, table)
            self._assert_second_migration_is_read_only(db_path)

    def test_legacy_repairs_run_once_per_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home, shared, db_path = populated_home(Path(td))
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with closing(self._open(db_path)) as conn:
                self.assertEqual(
                    get_meta(conn, "data_repair_version"), str(DATA_REPAIR_VERSION)
                )
                # A database from before the gate: rows needing every repair.
                set_meta(conn, "data_repair_version", None)
                conn.execute(
                    "INSERT INTO message_claims (claim_key, session_id) "
                    "VALUES ('msg-1:req-1', 'ghost')"
                )
                conn.execute(
                    "UPDATE sessions SET cache_creation_unknown_input_tokens = 3 "
                    "WHERE session_id = 'session-a'"
                )
                conn.commit()
                statements: list[str] = []
                conn.set_trace_callback(statements.append)
                migrate_db(conn)
                conn.set_trace_callback(None)
                conn.commit()
                self.assertTrue(
                    any(
                        "NOT IN (SELECT session_id FROM sessions)" in s
                        for s in statements
                    )
                )
                self.assertEqual(
                    rows(
                        conn,
                        "SELECT COUNT(*) FROM message_claims WHERE session_id = 'ghost'",
                    ),
                    [(0,)],
                )
                self.assertEqual(
                    rows(
                        conn,
                        "SELECT cache_creation_unknown_input_tokens FROM sessions "
                        "WHERE session_id = 'session-a'",
                    ),
                    [(0,)],
                )
                # Both other claimants of the orphan's key are owed a refresh.
                self.assertEqual(queue_contents(conn), ["session-a", "session-b"])

                statements.clear()
                conn.set_trace_callback(statements.append)
                migrate_db(conn)
                conn.set_trace_callback(None)
                self.assertFalse(
                    any(
                        "NOT IN (SELECT session_id FROM sessions)" in s
                        for s in statements
                    )
                )
            self._assert_second_migration_is_read_only(db_path)

    def test_drifted_views_and_triggers_are_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            with closing(self._open(db_path)) as conn:
                canonical = dict(
                    rows(
                        conn,
                        "SELECT name, sql FROM sqlite_master "
                        "WHERE type IN ('view', 'trigger') ORDER BY name",
                    )
                )
                conn.executescript(
                    """
                    DROP VIEW session_catalog;
                    CREATE VIEW session_catalog AS SELECT session_id FROM sessions;
                    DROP TRIGGER sessions_search_stale;
                    CREATE TRIGGER sessions_search_stale AFTER UPDATE ON sessions
                    BEGIN SELECT 1; END;
                    DROP TRIGGER sessions_message_claims_cleanup;
                    """
                )
                migrate_db(conn)
                conn.commit()
                self.assertEqual(
                    dict(
                        rows(
                            conn,
                            "SELECT name, sql FROM sqlite_master "
                            "WHERE type IN ('view', 'trigger') ORDER BY name",
                        )
                    ),
                    canonical,
                )
            self._assert_second_migration_is_read_only(db_path)

    def test_old_flag_left_at_zero_is_untouched_by_migration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            with get_db(db_path) as conn:
                set_meta(conn, "native_refresh_pending", "0")
            init_db(db_path)
            with get_db(db_path) as conn:
                self.assertEqual(get_meta(conn, "native_refresh_pending"), "0")
                self.assertEqual(queue_contents(conn), [])


# ── sync keeps unchanged derived rows ─────────────────────────────────────


class SyncWritesOnlyChangesTests(unittest.TestCase):
    TABLES = ("tool_calls", "session_paths", "session_daily_usage", "message_claims")

    def test_append_resync_only_adds_the_new_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home, shared, db_path = populated_home(Path(td))
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with closing(sqlite3.connect(db_path)) as conn:
                # Persistent triggers: sync runs on its own connection.
                conn.execute(
                    "CREATE TABLE write_log (tbl TEXT, op TEXT, session_id TEXT)"
                )
                for table in self.TABLES:
                    for op, row in (
                        ("INSERT", "new"),
                        ("UPDATE", "new"),
                        ("DELETE", "old"),
                    ):
                        # refresh_native_usage rewrites native_* of every
                        # day row it refreshes (the values, and so the pages,
                        # are unchanged); count only transcript columns.
                        event = (
                            f"UPDATE OF {', '.join(DAILY_COMPONENTS)}"
                            if (table, op) == ("session_daily_usage", "UPDATE")
                            else op
                        )
                        conn.execute(
                            f"""
                            CREATE TRIGGER log_{table}_{op.lower()}
                            AFTER {event} ON {table} BEGIN
                                INSERT INTO write_log
                                VALUES ('{table}', '{op}', {row}.session_id);
                            END
                            """
                        )
                conn.commit()
                ids_before = rows(
                    conn,
                    "SELECT id, tool_name FROM tool_calls "
                    "WHERE session_id = 'session-b' ORDER BY id",
                )
                paths_before = rows(
                    conn,
                    f"SELECT id, {PATH_COLUMNS} FROM session_paths "
                    "WHERE session_id = 'session-b' ORDER BY id",
                )

            project = home / ".claude" / "projects" / "-tmp-demo"
            write_jsonl(
                project / "session-b.jsonl", _claude_records("session-b", extra=5)
            )
            sync_sessions(shared, db_path, "alice", "machine-1", home)

            with closing(sqlite3.connect(db_path)) as conn:
                log = rows(
                    conn,
                    "SELECT tbl, op, session_id, COUNT(*) FROM write_log "
                    "GROUP BY tbl, op, session_id ORDER BY tbl, op, session_id",
                )
                # Two more assistant messages: two tool calls, two new paths,
                # two claims, one changed day. Nothing else is rewritten.
                self.assertEqual(
                    log,
                    [
                        ("message_claims", "INSERT", "session-b", 2),
                        ("session_daily_usage", "UPDATE", "session-b", 1),
                        ("session_paths", "INSERT", "session-b", 2),
                        ("tool_calls", "INSERT", "session-b", 2),
                    ],
                )
                self.assertEqual(
                    rows(
                        conn,
                        "SELECT id, tool_name FROM tool_calls "
                        "WHERE session_id = 'session-b' ORDER BY id",
                    )[: len(ids_before)],
                    ids_before,
                )
                self.assertEqual(
                    rows(
                        conn,
                        f"SELECT id, {PATH_COLUMNS} FROM session_paths "
                        "WHERE session_id = 'session-b' ORDER BY id",
                    )[: len(paths_before)],
                    paths_before,
                )


# ── transcript checkpoints ────────────────────────────────────────────────

checkpoints = st.builds(
    ParseCheckpoint,
    offset=st.integers(0, 2**62),
    prefix_sha256=st.text(alphabet="0123456789abcdef", min_size=64, max_size=64),
    generation=st.text(alphabet="0123456789abcdef", min_size=32, max_size=32),
    dev=st.integers(0, 2**31 - 1),
    ino=st.integers(0, 2**62),
    version=st.integers(0, 5),
)


class TranscriptCheckpointTests(unittest.TestCase):
    def _put(self, conn, source_path, checkpoint, *, now="2026-09-29T00:00:00Z", **kw):
        put_transcript_checkpoint(
            conn,
            source_path=source_path,
            session_id=kw.get("session_id", "s1"),
            source=kw.get("source", "claudecode"),
            checkpoint=checkpoint,
            state_file=kw.get("state_file", f"{source_path}.state"),
            file_mtime=kw.get("file_mtime", 12.5),
            now=now,
        )

    @PROPERTY_SETTINGS
    @given(
        first=checkpoints,
        second=checkpoints,
        mtime=st.one_of(st.none(), st.floats(0, 2e9)),
    )
    def test_round_trip_upsert_and_identical_put_writes_nothing(
        self, first, second, mtime
    ):
        conn = fresh_db()
        self._put(conn, "/t/a.jsonl", first, file_mtime=mtime)
        self.assertEqual(
            get_transcript_checkpoint(conn, "/t/a.jsonl"), (first, "/t/a.jsonl.state")
        )
        self._put(conn, "/t/a.jsonl", second, state_file="/t/b.state", file_mtime=mtime)
        self.assertEqual(
            get_transcript_checkpoint(conn, "/t/a.jsonl"), (second, "/t/b.state")
        )
        changes = conn.total_changes
        self._put(
            conn,
            "/t/a.jsonl",
            second,
            state_file="/t/b.state",
            file_mtime=mtime,
            now="2026-09-30T00:00:00Z",
        )
        self.assertEqual(conn.total_changes, changes)
        self.assertEqual(
            list(iter_transcript_checkpoints(conn)),
            [("/t/a.jsonl", "/t/b.state", mtime)],
        )

    def test_missing_and_deleted_checkpoints(self) -> None:
        conn = fresh_db()
        self.assertIsNone(get_transcript_checkpoint(conn, "/t/none.jsonl"))
        self._put(conn, "/t/a.jsonl", ParseCheckpoint(1, "a" * 64, "g", 1, 1))
        delete_transcript_checkpoint(conn, "/t/a.jsonl")
        self.assertIsNone(get_transcript_checkpoint(conn, "/t/a.jsonl"))
        delete_transcript_checkpoint(conn, "/t/a.jsonl")

    def test_iteration_survives_deleting_every_yielded_checkpoint(self) -> None:
        conn = fresh_db()
        paths = [f"/t/{index:05d}.jsonl" for index in range(1234)]
        for path in paths:
            self._put(
                conn, path, ParseCheckpoint(1, "a" * 64, "g", 1, 1), file_mtime=None
            )
        seen = []
        for source_path, state_file, mtime in iter_transcript_checkpoints(conn):
            seen.append(source_path)
            self.assertEqual(state_file, f"{source_path}.state")
            self.assertIsNone(mtime)
            delete_transcript_checkpoint(conn, source_path)
        self.assertEqual(seen, paths)
        self.assertEqual(list(iter_transcript_checkpoints(conn)), [])

    def test_schema_matches_the_contract(self) -> None:
        conn = fresh_db()
        self.assertEqual(
            rows(conn, "PRAGMA table_info(transcript_checkpoints)"),
            [
                (0, "source_path", "TEXT", 0, None, 1),
                (1, "session_id", "TEXT", 1, None, 0),
                (2, "source", "TEXT", 1, None, 0),
                (3, "dev", "INTEGER", 1, None, 0),
                (4, "ino", "INTEGER", 1, None, 0),
                (5, "parse_offset", "INTEGER", 1, None, 0),
                (6, "parse_prefix_sha256", "TEXT", 1, None, 0),
                (7, "parse_generation", "TEXT", 1, None, 0),
                (8, "parse_version", "INTEGER", 1, None, 0),
                (9, "state_file", "TEXT", 1, None, 0),
                (10, "file_mtime", "REAL", 0, None, 0),
                (11, "updated_at", "TEXT", 1, None, 0),
            ],
        )
        self.assertIn(
            ("idx_transcript_checkpoints_session",),
            rows(
                conn,
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'transcript_checkpoints'",
            ),
        )


if __name__ == "__main__":
    unittest.main()
