"""Appending search documents is exactly a full replacement of the same bytes.

The differential property: grow a transcript through arbitrary appends (cut
anywhere, including mid-line, with a final line that may lack its
terminator), call update_session_search_index after each append with a
fresh scan, and the documents, state and search results must equal a fresh
replace_session_search_index on the same bytes in a separate database.

The unit tests pin the fallbacks (public rows, prefix mismatch, missing
checkpoint), provisional tail retraction, and write-minimality (document ids
and a delete log). The backfill tests pin the SQL prefilter (exactly the
stale rows while the triggers hold), the chunked periodic verification with
its orphan heal, and the deadline.
"""

import contextlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from logpile import search
from logpile.db import (
    SEARCH_INDEX_VERSION,
    get_db,
    get_meta,
    init_db,
    migrate_db,
    set_meta,
)
from logpile.parsers import SearchTranscriptReadError
from logpile.search import (
    SEARCH_CHECKPOINT_COLUMNS,
    backfill_search_index,
    ensure_search_checkpoint_columns,
    replace_session_search_index,
    search_checkpoint_offset,
    search_sessions,
    update_session_search_index,
)
from logpile.transcript_io import scan_transcript

VOCABULARY = ["alpha", "bravo", "charlie", "delta", "echo", "needle"]
TEXT = st.sampled_from(
    [
        "alpha bravo",
        "charlie needle",
        "delta échelle ✓",
        "   ",
        "ok",
        "<system-reminder>hidden</system-reminder> echo ask",
        "echo " + "A" * 80,
        "bravo\ndelta across lines",
    ]
)
TERMINATORS = st.sampled_from(["\n", "\n", "\n", "\r\n", "\r"])


def _merge(*parts):
    merged = {}
    for part in parts:
        merged.update(part)
    return merged


claude_user = st.builds(
    lambda text, blocks, meta: _merge(
        {
            "type": "user",
            "message": {
                "content": [{"type": "text", "text": text}] if blocks else text
            },
        },
        {"isMeta": True} if meta else {},
    ),
    TEXT,
    st.booleans(),
    st.booleans(),
)
claude_tool_result = st.builds(
    lambda text: {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_a", "content": text},
                {"type": "text", "text": text},
            ]
        },
    },
    TEXT,
)
claude_assistant = st.builds(
    lambda text, thinking: {
        "type": "assistant",
        "message": {
            "id": "msg_a",
            "content": [
                {"type": "thinking", "thinking": thinking},
                {"type": "text", "text": text},
                {"type": "tool_use", "id": "toolu_a", "name": "Bash", "input": {}},
            ],
        },
    },
    TEXT,
    TEXT,
)
claude_other = st.sampled_from(
    [
        {"type": "summary", "summary": "alpha summary"},
        {"type": "system", "content": "bravo compacted"},
        {"type": "assistant", "message": "not a dict"},
    ]
)
claude_records = st.lists(
    st.one_of(claude_user, claude_tool_result, claude_assistant, claude_other),
    max_size=12,
)


def _codex_message(role, block, text, shape):
    if shape == "response_item":
        return {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": role,
                "content": [{"type": block, "text": text}],
            },
        }
    if shape == "legacy_bare_block":
        return {"type": "message", "role": role, "content": [{"text": text}]}
    return {"type": "message", "role": role, "content": text}


codex_message = st.builds(
    _codex_message,
    st.sampled_from(["user", "assistant", "developer", "system"]),
    st.sampled_from(["input_text", "output_text", "text", "image"]),
    st.one_of(
        TEXT,
        st.just("# AGENTS.md instructions for /Users/me/repo\n\n<INSTRUCTIONS>x"),
        st.just(
            "# Context from my IDE setup:\nfoo\n## My request for Codex:\ndelta ide ask"
        ),
        st.just("<environment_context>cwd</environment_context>"),
    ),
    st.sampled_from(["response_item", "legacy_bare_block", "legacy_string"]),
)
codex_other = st.sampled_from(
    [
        {"type": "session_meta", "payload": {"id": "leaf", "cwd": "/Users/me"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "x"}},
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "shell", "call_id": "c"},
        },
    ]
)
codex_records = st.lists(
    st.one_of(codex_message, codex_message, codex_other), max_size=12
)
# Lines that are not records: a torn write, a blank line, and a line carrying
# an invalid UTF-8 byte inside an otherwise valid string.
JUNK_LINES = [
    b'{"truncated": ',
    b"",
    b'{"type": "user", "message": {"content": "\xff echo"}}',
    # A multi-byte sequence cut short inside a string and at the line end.
    b'{"type": "user", "message": {"content": "echo \xe2\x82"}}',
    b'{"type": "user", "message": {"content": "echo"}}\xe2\x82',
]


def _serialize(records, terminators, trailing, junk):
    lines = [
        json.dumps(record, ensure_ascii=False).encode("utf-8") for record in records
    ]
    for position, line in junk:
        lines.insert(position % (len(lines) + 1), line)
    data = bytearray()
    for index, line in enumerate(lines):
        data += line
        if index < len(lines) - 1 or trailing:
            data += terminators[index % len(terminators)].encode("ascii")
    return bytes(data)


def _cut_position(data, cut):
    """Byte length of the file after one append step.

    ``None`` means the whole file. Cuts just before a terminator leave a
    complete record without its newline (provisional documents); cuts just
    after one land on a checkpoint; anything else can tear a line.
    """
    if cut is None:
        return len(data)
    mode, value = cut
    ends = [index for index, byte in enumerate(data) if byte in b"\r\n"]
    if mode == "anywhere" or not ends:
        return value % (len(data) + 1)
    return ends[value % len(ends)] + (mode == "after_terminator")


def _file_revisions(primary, alternate, cuts, rewrites):
    """File contents after each step, ending with the whole of ``primary``.

    Steps grow ``primary`` monotonically (true appends), except rewrite
    steps, which put a prefix of ``alternate`` in place instead: a same-inode
    rewrite that is not an append, after which growth of ``primary`` is not
    an append either.
    """
    lengths = sorted(_cut_position(primary, cut) for cut in cuts)
    revisions = [primary[:length] for length in lengths] + [primary]
    for index in rewrites:
        if index < len(cuts):
            revisions[index] = alternate[: _cut_position(alternate, cuts[index])]
    return revisions


STEPS = st.lists(
    st.tuples(
        st.sampled_from(["anywhere", "before_terminator", "after_terminator"]),
        st.integers(0, 10**6),
    ),
    min_size=1,
    max_size=6,
)


_TEMPLATE: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    """A fresh migrated in-memory database (copied from a migrated template)."""
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


def _insert_session(
    conn,
    session_id,
    *,
    source="claudecode",
    path=None,
    goal="",
    visibility="private",
):
    conn.execute(
        """
        INSERT INTO sessions (
            session_id, source, username, source_path, shared_path,
            first_timestamp, session_goal, repo_name, project, visibility
        ) VALUES (?, ?, 'alice', ?, ?, '2026-09-29T00:00:00Z', ?, 'demo-repo',
                  'demo-project', ?)
        """,
        (session_id, source, str(path or ""), str(path or ""), goal, visibility),
    )


def _set_revision(conn, session_id, scan, **columns):
    # Mirrors the sync upsert: a new file_hash fires sessions_search_stale.
    assignments = {"file_hash": scan.sha256, "file_size": scan.size, **columns}
    conn.execute(
        "UPDATE sessions SET {} WHERE session_id = ?".format(
            ", ".join(f"{column} = ?" for column in assignments)
        ),
        (*assignments.values(), session_id),
    )


def _index(conn, session_id, path, *, request_offset=True, **columns):
    """What sync does per changed transcript: scan, upsert, update."""
    offsets = [search_checkpoint_offset(conn, session_id)] if request_offset else []
    scan = scan_transcript(path, prefix_offsets=offsets)
    _set_revision(conn, session_id, scan, **columns)
    update_session_search_index(conn, session_id, transcript_path=path, scan=scan)
    return scan


def _documents(conn, session_id):
    return {
        (row["field_label"], row["chunk_index"]): (
            row["structured_text"],
            row["transcript_text"],
        )
        for row in conn.execute(
            "SELECT * FROM session_search_documents WHERE session_id = ?",
            (session_id,),
        )
    }


def _ids(conn, session_id):
    return {
        (row["field_label"], row["chunk_index"]): row["id"]
        for row in conn.execute(
            "SELECT id, field_label, chunk_index FROM session_search_documents "
            "WHERE session_id = ?",
            (session_id,),
        )
    }


def _order(conn, session_id):
    """Document keys in id order within each ranking tier.

    search_sessions keeps a session's best (tier, score, id), so id order is
    a tiebreak only among documents of one tier (structured_text or
    transcript_text column); order across tiers never matters.
    """
    keys = [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT field_label, chunk_index FROM session_search_documents "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
    ]
    transcript = [key for key in keys if key[0].startswith("transcript_")]
    structured = [key for key in keys if not key[0].startswith("transcript_")]
    return structured, transcript


def _state(conn, session_id):
    row = conn.execute(
        "SELECT * FROM session_search_state WHERE session_id = ?", (session_id,)
    ).fetchone()
    return None if row is None else dict(row)


def _common_state(conn, session_id):
    state = _state(conn, session_id)
    for column in ("indexed_at", *(name for name, _spec in SEARCH_CHECKPOINT_COLUMNS)):
        state.pop(column)
    return state


def _checkpoint(conn, session_id):
    state = _state(conn, session_id)
    return tuple(state[name] for name, _spec in SEARCH_CHECKPOINT_COLUMNS)


def _is_current(conn, session_id):
    row = conn.execute(
        f"{search._BACKFILL_ROWS_SQL} WHERE s.session_id = ?", (session_id,)
    ).fetchone()
    return search._state_is_current(row)


def _reference_documents(path, *, source="claudecode", goal="", size=None):
    """Documents of a fresh full replacement of ``path`` (or its first bytes)."""
    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / "reference.jsonl"
        data = path.read_bytes()
        copy.write_bytes(data if size is None else data[:size])
        conn = _connect()
        _insert_session(conn, "reference", source=source, path=copy, goal=goal)
        replace_session_search_index(conn, "reference", transcript_path=copy)
        return _documents(conn, "reference")


def _assert_fts_consistent(conn):
    conn.execute(
        "INSERT INTO session_search_fts(session_search_fts) VALUES('integrity-check')"
    )


def _line(record) -> bytes:
    return json.dumps(record).encode("utf-8") + b"\n"


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _assistant(text):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


class _DeleteLog:
    """Temp trigger recording every document delete (each one makes FTS5
    re-tokenize the deleted text)."""

    def __init__(self, conn):
        self.conn = conn
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS deleted_documents "
            "(session_id TEXT, field_label TEXT, chunk_index INTEGER)"
        )
        conn.execute(
            """
            CREATE TEMP TRIGGER IF NOT EXISTS log_document_delete
            AFTER DELETE ON main.session_search_documents BEGIN
                INSERT INTO deleted_documents
                VALUES (old.session_id, old.field_label, old.chunk_index);
            END
            """
        )

    def take(self):
        rows = [
            (row[0], row[1])
            for row in self.conn.execute(
                "SELECT field_label, chunk_index FROM deleted_documents ORDER BY rowid"
            )
        ]
        self.conn.execute("DELETE FROM deleted_documents")
        return rows


class IncrementalSearchPropertyTests(unittest.TestCase):
    def _run_differential(self, source, revisions, goals):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "session.jsonl"
            path.write_bytes(b"")
            peer = Path(td) / "peer.jsonl"
            peer.write_bytes(
                b'{"type": "user", "message": {"content": "alpha peer"}}\n'
                b'{"type": "response_item", "payload": {"type": "message", '
                b'"role": "assistant", "content": [{"type": "output_text", '
                b'"text": "alpha peer"}]}}\n'
            )
            incremental = _connect()
            _insert_session(incremental, "grow", source=source, path=path)
            _insert_session(incremental, "peer", source=source, path=peer)
            replace_session_search_index(incremental, "peer", transcript_path=peer)
            written = b""
            resumed = retracted = 0
            for step, (content, goal) in enumerate(zip(revisions, goals, strict=False)):
                if content.startswith(written):
                    with path.open("ab") as handle:
                        handle.write(content[len(written) :])
                else:
                    # Same-inode truncate and rewrite: not an append.
                    with path.open("r+b") as handle:
                        handle.truncate(0)
                        handle.write(content)
                previous, written = written, content

                before = _state(incremental, "grow")
                before_ids = _ids(incremental, "grow")
                scan = _index(incremental, "grow", path, session_goal=goal)

                # A separate, freshly migrated database indexes the same bytes
                # with one full replacement.
                reference = _connect()
                _insert_session(reference, "peer", source=source, path=peer)
                replace_session_search_index(reference, "peer", transcript_path=peer)
                _insert_session(reference, "grow", source=source, path=path)
                _set_revision(reference, "grow", scan, session_goal=goal)
                replace_session_search_index(reference, "grow", transcript_path=path)

                label = f"step {step} ({len(written)} bytes)"
                self.assertEqual(
                    _documents(incremental, "grow"),
                    _documents(reference, "grow"),
                    label,
                )
                self.assertEqual(
                    _order(incremental, "grow"), _order(reference, "grow"), label
                )
                self.assertEqual(
                    _common_state(incremental, "grow"),
                    _common_state(reference, "grow"),
                    label,
                )
                self.assertTrue(_is_current(incremental, "grow"), label)
                self.assertEqual(
                    _checkpoint(incremental, "grow")[:2],
                    (scan.line_end, scan.line_end_sha256),
                    label,
                )

                # Whenever the old checkpoint prefix survived, every settled
                # document must have been kept, not rewritten.
                old_offset = before and before["transcript_offset"]
                if old_offset is not None and (
                    written[:old_offset] == previous[:old_offset]
                    and len(written) >= old_offset
                ):
                    resumed += 1
                    after_ids = _ids(incremental, "grow")
                    for role in ("user", "assistant"):
                        settled = before[f"{role}_chunks"]
                        for key, doc_id in before_ids.items():
                            if key[0] != f"transcript_{role}":
                                continue
                            if key[1] < settled:
                                self.assertEqual(after_ids.get(key), doc_id, label)
                            else:
                                retracted += 1

                _assert_fts_consistent(incremental)
                for word in VOCABULARY:
                    self.assertEqual(
                        search_sessions(incremental, word),
                        search_sessions(reference, word),
                        f"{label}: {word}",
                    )
            event(f"resumed steps={min(resumed, 3)}")
            event(f"provisional documents retracted={min(retracted, 2)}")

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(
        records=claude_records,
        alternate_records=claude_records,
        terminators=st.lists(TERMINATORS, min_size=1, max_size=4),
        trailing=st.booleans(),
        junk=st.lists(
            st.tuples(st.integers(0, 20), st.sampled_from(JUNK_LINES)), max_size=2
        ),
        steps=STEPS,
        rewrites=st.lists(st.integers(0, 5), max_size=1),
        goals=st.lists(st.sampled_from(["", "alpha goal", "needle goal"]), min_size=7),
    )
    def test_claude_appends_equal_full_replace(
        self,
        records,
        alternate_records,
        terminators,
        trailing,
        junk,
        steps,
        rewrites,
        goals,
    ):
        primary = _serialize(records, terminators, trailing, junk)
        alternate = _serialize(alternate_records, terminators, trailing, junk)
        revisions = _file_revisions(primary, alternate, steps, rewrites)
        self._run_differential("claudecode", revisions, goals)

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(
        records=codex_records,
        alternate_records=codex_records,
        terminators=st.lists(TERMINATORS, min_size=1, max_size=4),
        trailing=st.booleans(),
        junk=st.lists(
            st.tuples(st.integers(0, 20), st.sampled_from(JUNK_LINES)), max_size=2
        ),
        steps=STEPS,
        rewrites=st.lists(st.integers(0, 5), max_size=1),
        goals=st.lists(st.sampled_from(["", "alpha goal", "needle goal"]), min_size=7),
    )
    def test_codex_appends_equal_full_replace(
        self,
        records,
        alternate_records,
        terminators,
        trailing,
        junk,
        steps,
        rewrites,
        goals,
    ):
        primary = _serialize(records, terminators, trailing, junk)
        alternate = _serialize(alternate_records, terminators, trailing, junk)
        revisions = _file_revisions(primary, alternate, steps, rewrites)
        self._run_differential("codex", revisions, goals)


class IncrementalSearchUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "session.jsonl"
        self.conn = _connect()
        _insert_session(self.conn, "s", path=self.path, goal="alpha goal")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _append(self, data: bytes) -> None:
        with self.path.open("ab") as handle:
            handle.write(data)

    def _assert_equals_full_replace(self, **kwargs):
        goal = self.conn.execute(
            "SELECT session_goal FROM sessions WHERE session_id = 's'"
        ).fetchone()[0]
        self.assertEqual(
            _documents(self.conn, "s"),
            _reference_documents(self.path, goal=goal, **kwargs),
        )
        _assert_fts_consistent(self.conn)

    def test_append_keeps_settled_documents_and_moves_checkpoint(self):
        self.path.write_bytes(
            _line(_user("alpha one")) + _line(_assistant("bravo two"))
        )
        first = _index(self.conn, "s", self.path)
        self.assertEqual(_checkpoint(self.conn, "s"), (first.size, first.sha256, 1, 1))
        settled = _ids(self.conn, "s")
        log = _DeleteLog(self.conn)

        self._append(_line(_user("charlie three")))
        second = _index(self.conn, "s", self.path)

        self.assertEqual(log.take(), [])
        ids = _ids(self.conn, "s")
        self.assertEqual({key: ids[key] for key in settled}, settled)
        self.assertIn(("transcript_user", 1), ids)
        self.assertEqual(
            _checkpoint(self.conn, "s"), (second.size, second.sha256, 2, 1)
        )
        self.assertEqual(_state(self.conn, "s")["transcript_status"], "complete")
        self.assertTrue(_is_current(self.conn, "s"))
        self._assert_equals_full_replace()

    def test_tail_documents_are_retracted_when_the_line_is_completed(self):
        head = _line(_user("alpha one"))
        tail = json.dumps(_user("provisional needle")).encode()
        self.path.write_bytes(head + tail)
        scan = _index(self.conn, "s", self.path)
        # The unterminated record is indexed like a full replacement would,
        # but past the checkpoint.
        self.assertEqual(scan.line_end, len(head))
        self.assertEqual(_checkpoint(self.conn, "s")[0::2], (len(head), 1))
        self.assertEqual(
            _documents(self.conn, "s")[("transcript_user", 1)],
            (None, "provisional needle"),
        )
        self.assertTrue(search_sessions(self.conn, "provisional needle"))
        log = _DeleteLog(self.conn)

        self._append(b"\n")
        _index(self.conn, "s", self.path)
        self.assertEqual(log.take(), [("transcript_user", 1)])
        self.assertEqual(_checkpoint(self.conn, "s")[0::2], (len(head + tail) + 1, 2))
        self._assert_equals_full_replace()

    def test_tail_documents_disappear_when_the_line_turns_out_invalid(self):
        head = _line(_user("alpha one"))
        tail = json.dumps(_user("provisional needle")).encode()
        self.path.write_bytes(head + tail)
        _index(self.conn, "s", self.path)
        self.assertTrue(search_sessions(self.conn, "provisional needle"))

        # The writer had not finished the line: what looked like a complete
        # record is the start of a longer, here malformed, one.
        self._append(b' trailing"}\n' + _line(_assistant("bravo after")))
        _index(self.conn, "s", self.path)
        self.assertEqual(search_sessions(self.conn, "provisional needle"), [])
        self.assertTrue(search_sessions(self.conn, "bravo after"))
        self._assert_equals_full_replace()

    def test_structured_documents_untouched_when_metadata_unchanged(self):
        self.path.write_bytes(_line(_user("alpha one")))
        _index(self.conn, "s", self.path)
        structured = {
            key: doc_id
            for key, doc_id in _ids(self.conn, "s").items()
            if not key[0].startswith("transcript_")
        }
        self.assertIn(("session_goal", 0), structured)
        log = _DeleteLog(self.conn)

        self._append(_line(_user("bravo two")))
        _index(self.conn, "s", self.path)
        self.assertEqual(log.take(), [])
        ids = _ids(self.conn, "s")
        self.assertEqual({key: ids[key] for key in structured}, structured)

        # A metadata change rewrites the structured rows (all of them, in
        # field order) and still leaves every transcript row alone.
        transcript = {k: v for k, v in ids.items() if k[0].startswith("transcript_")}
        _index(self.conn, "s", self.path, session_goal="charlie goal")
        deleted = log.take()
        self.assertTrue(deleted)
        self.assertTrue(
            all(not label.startswith("transcript_") for label, _ in deleted)
        )
        ids = _ids(self.conn, "s")
        self.assertEqual({key: ids[key] for key in transcript}, transcript)
        self.assertEqual(
            search_sessions(self.conn, "charlie goal")[0]["matched_field"],
            "session goal",
        )
        self.assertEqual(search_sessions(self.conn, "alpha goal"), [])
        self._assert_equals_full_replace()

    def test_prefix_mismatch_falls_back_to_full_replace(self):
        self.path.write_bytes(_line(_user("alpha one")) + _line(_user("bravo two")))
        _index(self.conn, "s", self.path)
        old = _ids(self.conn, "s")
        log = _DeleteLog(self.conn)

        # Same-inode rewrite that also grows: growth alone is not an append.
        with self.path.open("r+b") as handle:
            handle.write(
                _line(_user("ALPHA ONE"))
                + _line(_user("bravo two"))
                + _line(_user("charlie three"))
            )
        _index(self.conn, "s", self.path)
        deleted = set(log.take())
        self.assertEqual(deleted, set(old))
        self.assertEqual(search_sessions(self.conn, "alpha one")[0]["session_id"], "s")
        self._assert_equals_full_replace()
        self.assertTrue(_is_current(self.conn, "s"))

    def test_scan_without_the_checkpoint_offset_falls_back_to_full_replace(self):
        self.path.write_bytes(_line(_user("alpha one")))
        _index(self.conn, "s", self.path)
        log = _DeleteLog(self.conn)
        self._append(_line(_user("bravo two")))

        _index(self.conn, "s", self.path, request_offset=False)
        self.assertIn(("transcript_user", 0), log.take())
        self._assert_equals_full_replace()
        # The full path recorded a fresh checkpoint, so the next append resumes.
        self._append(_line(_user("charlie three")))
        _index(self.conn, "s", self.path)
        self.assertEqual(log.take(), [])
        self._assert_equals_full_replace()

    def test_legacy_state_without_checkpoint_takes_one_full_replace(self):
        self.path.write_bytes(_line(_user("alpha one")))
        scan = scan_transcript(self.path)
        _set_revision(self.conn, "s", scan)
        replace_session_search_index(self.conn, "s", transcript_path=self.path)
        self.assertEqual(_checkpoint(self.conn, "s"), (None, None, None, None))
        self.assertIsNone(search_checkpoint_offset(self.conn, "s"))
        log = _DeleteLog(self.conn)

        self._append(_line(_user("bravo two")))
        _index(self.conn, "s", self.path)
        self.assertIn(("transcript_user", 0), log.take())
        self._append(_line(_user("charlie three")))
        _index(self.conn, "s", self.path)
        self.assertEqual(log.take(), [])
        self._assert_equals_full_replace()

        # And a plain replacement (no scan) clears the checkpoint again.
        replace_session_search_index(self.conn, "s", transcript_path=self.path)
        self.assertEqual(_checkpoint(self.conn, "s"), (None, None, None, None))

    def test_error_state_keeps_documents_and_resumes(self):
        self.path.write_bytes(_line(_user("alpha one")))
        _index(self.conn, "s", self.path)
        checkpoint = _checkpoint(self.conn, "s")
        settled = _ids(self.conn, "s")
        row = search._session_row(self.conn, "s")
        search._record_search_error(self.conn, row, status="error", error="boom")
        self.assertEqual(_state(self.conn, "s")["transcript_status"], "error")
        self.assertEqual(_checkpoint(self.conn, "s"), checkpoint)

        self._append(_line(_user("bravo two")))
        _index(self.conn, "s", self.path)
        ids = _ids(self.conn, "s")
        self.assertEqual({key: ids[key] for key in settled}, settled)
        self._assert_equals_full_replace()

    def test_bytes_past_the_scan_are_never_indexed(self):
        self.path.write_bytes(_line(_user("alpha one")))
        _index(self.conn, "s", self.path)
        self._append(_line(_user("bravo two")))
        scan = scan_transcript(
            self.path, prefix_offsets=[search_checkpoint_offset(self.conn, "s")]
        )
        _set_revision(self.conn, "s", scan)
        # A live writer appends after the scan and before indexing.
        self._append(_line(_user("late needle")))
        update_session_search_index(
            self.conn, "s", transcript_path=self.path, scan=scan
        )
        self.assertEqual(search_sessions(self.conn, "late needle"), [])
        self._assert_equals_full_replace(size=scan.size)

    def test_file_shorter_than_its_scan_raises_and_keeps_the_revision(self):
        self.path.write_bytes(_line(_user("alpha one")))
        _index(self.conn, "s", self.path)
        before_docs = _documents(self.conn, "s")
        before_state = _state(self.conn, "s")
        self._append(_line(_user("bravo two")))
        scan = scan_transcript(
            self.path, prefix_offsets=[search_checkpoint_offset(self.conn, "s")]
        )
        with self.path.open("r+b") as handle:
            handle.truncate(scan.size - 3)
        with self.assertRaises(SearchTranscriptReadError):
            update_session_search_index(
                self.conn, "s", transcript_path=self.path, scan=scan
            )
        self.assertEqual(_documents(self.conn, "s"), before_docs)
        self.assertEqual(_state(self.conn, "s"), before_state)

    def test_unreadable_transcript_raises_search_error(self):
        self.path.write_bytes(_line(_user("alpha one")))
        scan = scan_transcript(self.path)
        self.path.unlink()
        with self.assertRaises(SearchTranscriptReadError):
            update_session_search_index(
                self.conn, "s", transcript_path=self.path, scan=scan
            )
        self.assertIsNone(_state(self.conn, "s"))

    def test_missing_session_row_removes_the_index(self):
        self.path.write_bytes(_line(_user("alpha one")))
        _index(self.conn, "s", self.path)
        self.conn.execute("DROP TRIGGER sessions_search_cleanup")
        self.conn.execute("DELETE FROM sessions WHERE session_id = 's'")
        scan = scan_transcript(self.path)
        result = update_session_search_index(
            self.conn, "s", transcript_path=self.path, scan=scan
        )
        self.assertEqual(result, "removed")
        self.assertEqual(_documents(self.conn, "s"), {})
        self.assertIsNone(_state(self.conn, "s"))

    def test_public_rows_take_the_verified_artifact_path(self):
        self.path.write_bytes(_line(_user("private transcript needle")))
        _index(self.conn, "s", self.path)
        self.assertIsNotNone(search_checkpoint_offset(self.conn, "s"))
        self.conn.execute(
            "UPDATE sessions SET visibility = 'public', reviewed_sha256 = 'reviewed'"
            " WHERE session_id = 's'"
        )
        scan = scan_transcript(self.path)
        with self.assertRaisesRegex(SearchTranscriptReadError, "shared directory"):
            update_session_search_index(
                self.conn, "s", transcript_path=self.path, scan=scan
            )

        seen = []

        @contextlib.contextmanager
        def artifact(row, *, shared_dir):
            seen.append((row["session_id"], shared_dir))
            yield io.StringIO(_line(_user("reviewed artifact needle")).decode())

        with mock.patch("logpile.publish.open_verified_public_artifact", artifact):
            update_session_search_index(
                self.conn,
                "s",
                transcript_path=self.path,
                scan=scan,
                shared_dir=self.root,
            )
        self.assertEqual(seen, [("s", self.root)])
        state = _state(self.conn, "s")
        self.assertEqual(state["artifact_hash"], "reviewed")
        self.assertEqual(_checkpoint(self.conn, "s"), (None, None, None, None))
        self.assertTrue(search_sessions(self.conn, "reviewed artifact needle"))
        self.assertEqual(search_sessions(self.conn, "private transcript needle"), [])

        # Back to private: artifact-derived documents are never resumed.
        self.conn.execute(
            "UPDATE sessions SET visibility = 'private' WHERE session_id = 's'"
        )
        log = _DeleteLog(self.conn)
        _index(self.conn, "s", self.path)
        self.assertIn(("transcript_user", 0), log.take())
        self.assertEqual(search_sessions(self.conn, "reviewed artifact needle"), [])
        self._assert_equals_full_replace()


class _RacingConnection(sqlite3.Connection):
    """Runs a callback just before the indexing savepoint opens."""

    race_hook = None

    def execute(self, sql, *args):
        if self.race_hook is not None and str(sql).startswith(
            "SAVEPOINT replace_session_search_index"
        ):
            hook, self.race_hook = self.race_hook, None
            hook()
        return super().execute(sql, *args)


class IncrementalSearchRaceTests(unittest.TestCase):
    def test_session_mutation_during_update_defers_and_keeps_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            path = root / "session.jsonl"
            path.write_bytes(_line(_user("first revision text")))
            init_db(db_path)
            with get_db(db_path) as conn:
                _insert_session(conn, "racing", path=path, goal="original goal")
                _index(conn, "racing", path)
                checkpoint = _checkpoint(conn, "racing")
                documents = _documents(conn, "racing")
            with path.open("ab") as handle:
                handle.write(_line(_user("second revision text")))

            racing = sqlite3.connect(db_path, factory=_RacingConnection)
            racing.row_factory = sqlite3.Row
            try:

                def commit_redaction():
                    other = sqlite3.connect(db_path)
                    try:
                        other.execute(
                            "UPDATE sessions SET session_goal = 'redacted goal' "
                            "WHERE session_id = 'racing'"
                        )
                        other.commit()
                    finally:
                        other.close()

                scan = scan_transcript(
                    path, prefix_offsets=[search_checkpoint_offset(racing, "racing")]
                )
                racing.execute(
                    "UPDATE sessions SET file_hash = ? WHERE session_id = 'racing'",
                    (scan.sha256,),
                )
                racing.commit()
                racing.race_hook = commit_redaction
                with self.assertRaises(SearchTranscriptReadError):
                    update_session_search_index(
                        racing, "racing", transcript_path=path, scan=scan
                    )
            finally:
                racing.close()

            with get_db(db_path) as conn:
                self.assertEqual(_documents(conn, "racing"), documents)
                self.assertEqual(_checkpoint(conn, "racing"), checkpoint)
                self.assertEqual(_state(conn, "racing")["transcript_status"], "stale")
                self.assertEqual(search_sessions(conn, "redacted goal"), [])


class SearchBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = _connect()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _session(self, session_id, *texts, goal=""):
        path = self.root / f"{session_id}.jsonl"
        path.write_bytes(b"".join(_line(_user(text)) for text in texts))
        _insert_session(self.conn, session_id, path=path, goal=goal)
        scan = scan_transcript(path)
        _set_revision(self.conn, session_id, scan)
        return path

    def _age_verification(self, days=8):
        stamp = datetime.now(UTC) - timedelta(days=days)
        set_meta(self.conn, "search_full_verify_at", stamp.isoformat())
        self.conn.commit()

    def test_backfill_records_a_resumable_checkpoint(self):
        path = self._session("a", "alpha one")
        stats = backfill_search_index(self.conn)
        self.assertEqual((stats.indexed, stats.errors), (1, 0))
        scan = scan_transcript(path)
        self.assertEqual(_checkpoint(self.conn, "a"), (scan.size, scan.sha256, 1, 0))
        settled = _ids(self.conn, "a")
        with path.open("ab") as handle:
            handle.write(_line(_user("bravo two")))
        _index(self.conn, "a", path)
        ids = _ids(self.conn, "a")
        self.assertEqual({key: ids[key] for key in settled}, settled)

    def test_up_to_date_corpus_hashes_no_rows(self):
        for name in ("a", "b", "c"):
            self._session(name, f"{name} text")
        first = backfill_search_index(self.conn)
        self.assertEqual(first.indexed, 3)
        self.assertEqual(
            get_meta(self.conn, "search_full_verify_version"), str(SEARCH_INDEX_VERSION)
        )
        with mock.patch.object(
            search, "search_metadata_hash", wraps=search.search_metadata_hash
        ) as hashed:
            stats = backfill_search_index(self.conn)
        self.assertEqual(hashed.call_count, 0)
        self.assertEqual((stats.scanned, stats.indexed, stats.deferred), (0, 0, 0))

    def test_metadata_only_change_is_found_and_resumed(self):
        self._session("a", "alpha one", "bravo two", goal="old goal")
        self._session("b", "charlie")
        backfill_search_index(self.conn)
        transcript = {
            key: doc_id
            for key, doc_id in _ids(self.conn, "a").items()
            if key[0].startswith("transcript_")
        }
        self.conn.execute(
            "UPDATE sessions SET session_goal = 'new goal' WHERE session_id = 'a'"
        )
        stats = backfill_search_index(self.conn)
        self.assertEqual((stats.scanned, stats.indexed), (1, 1))
        ids = _ids(self.conn, "a")
        self.assertEqual({key: ids[key] for key in transcript}, transcript)
        self.assertTrue(search_sessions(self.conn, "new goal"))
        self.assertEqual(search_sessions(self.conn, "old goal"), [])
        self.assertTrue(_is_current(self.conn, "a"))

    def test_periodic_full_verification_heals_a_trigger_bypass(self):
        self._session("a", "alpha one", goal="old goal")
        backfill_search_index(self.conn)
        self.conn.execute("DROP TRIGGER sessions_search_stale")
        self.conn.execute(
            "UPDATE sessions SET session_goal = 'bypassed goal' WHERE session_id = 'a'"
        )
        # The prefilter trusts the trigger, so a recent verification misses it.
        stats = backfill_search_index(self.conn)
        self.assertEqual(stats.scanned, 0)
        self.assertTrue(search_sessions(self.conn, "old goal"))

        self._age_verification()
        stats = backfill_search_index(self.conn)
        self.assertEqual((stats.scanned, stats.indexed), (1, 1))
        self.assertTrue(search_sessions(self.conn, "bypassed goal"))
        self.assertEqual(search_sessions(self.conn, "old goal"), [])

        # A version change also forces a full pass.
        self.conn.execute(
            "UPDATE sessions SET session_goal = 'second bypass' WHERE session_id = 'a'"
        )
        set_meta(self.conn, "search_full_verify_version", "0")
        stats = backfill_search_index(self.conn)
        self.assertEqual(stats.indexed, 1)
        self.assertTrue(search_sessions(self.conn, "second bypass"))

    def test_orphans_are_healed_only_by_the_verification_pass(self):
        self._session("a", "alpha one")
        self._session("gone", "orphan needle")
        backfill_search_index(self.conn)
        self.conn.execute("DROP TRIGGER sessions_search_cleanup")
        self.conn.execute("DELETE FROM sessions WHERE session_id = 'gone'")
        self.assertTrue(_documents(self.conn, "gone"))
        # Orphans are unreachable from search, which joins session_catalog.
        self.assertEqual(search_sessions(self.conn, "orphan needle"), [])

        backfill_search_index(self.conn)
        self.assertTrue(_documents(self.conn, "gone"))
        self.assertIsNotNone(_state(self.conn, "gone"))

        self._age_verification()
        backfill_search_index(self.conn)
        self.assertEqual(_documents(self.conn, "gone"), {})
        self.assertIsNone(_state(self.conn, "gone"))
        self.assertTrue(_documents(self.conn, "a"))
        _assert_fts_consistent(self.conn)

    def test_deadline_stops_after_a_batch_and_a_later_call_finishes(self):
        names = [f"s{index}" for index in range(5)]
        # One load for all candidates, and one load per batch.
        for chunk in (2000, 2):
            with (
                self.subTest(chunk=chunk),
                mock.patch.object(search, "_BACKFILL_CHUNK", chunk),
            ):
                self.conn.close()
                self.conn = _connect()
                for name in names:
                    self._session(name, f"{name} text")
                expired = time.monotonic() - 1
                first = backfill_search_index(self.conn, batch_size=2, deadline=expired)
                self.assertEqual((first.indexed, first.deferred), (2, 3))
                # An interrupted pass is not recorded as a verification.
                self.assertIsNone(get_meta(self.conn, "search_full_verify_at"))
                self.assertFalse(self.conn.in_transaction)

                second = backfill_search_index(
                    self.conn, batch_size=2, deadline=expired
                )
                self.assertEqual((second.indexed, second.deferred), (2, 1))
                final = backfill_search_index(self.conn, batch_size=2)
                self.assertEqual((final.indexed, final.deferred), (1, 0))
                self.assertIsNotNone(get_meta(self.conn, "search_full_verify_at"))
                self.assertTrue(all(_is_current(self.conn, name) for name in names))

    def test_verification_resumes_across_calls_one_chunk_at_a_time(self):
        names = [f"s{index}" for index in range(5)]
        for name in names:
            self._session(name, f"{name} text", goal=f"{name} goal")
        backfill_search_index(self.conn)
        # An orphan between s2 and s3, and two changes the trigger missed.
        self._session("s2a", "orphan needle")
        _index(self.conn, "s2a", self.root / "s2a.jsonl")
        self.conn.execute("DROP TRIGGER sessions_search_cleanup")
        self.conn.execute("DROP TRIGGER sessions_search_stale")
        self.conn.execute("DELETE FROM sessions WHERE session_id = 's2a'")
        for name in ("s0", "s4"):
            self.conn.execute(
                "UPDATE sessions SET session_goal = 'bypassed goal' "
                "WHERE session_id = ?",
                (name,),
            )
        self._age_verification()
        aged = get_meta(self.conn, "search_full_verify_at")
        expired = time.monotonic() - 1

        with mock.patch.object(search, "_BACKFILL_CHUNK", 2):
            first = backfill_search_index(self.conn, deadline=expired)
            self.assertEqual((first.scanned, first.indexed), (2, 1))
            cursor = f"{SEARCH_INDEX_VERSION}:s1"
            self.assertEqual(get_meta(self.conn, "search_full_verify_cursor"), cursor)
            self.assertTrue(_documents(self.conn, "s2a"))

            second = backfill_search_index(self.conn, deadline=expired)
            self.assertEqual((second.scanned, second.indexed), (2, 0))
            self.assertEqual(_documents(self.conn, "s2a"), {})
            self.assertIsNone(_state(self.conn, "s2a"))
            self.assertEqual(get_meta(self.conn, "search_full_verify_at"), aged)

            third = backfill_search_index(self.conn, deadline=expired)
            self.assertEqual((third.scanned, third.indexed), (1, 1))
            self.assertIsNone(get_meta(self.conn, "search_full_verify_cursor"))
            self.assertNotEqual(get_meta(self.conn, "search_full_verify_at"), aged)

            fourth = backfill_search_index(self.conn, deadline=expired)
            self.assertEqual(fourth.scanned, 0)
        self.assertEqual(
            {row["session_id"] for row in search_sessions(self.conn, "bypassed goal")},
            {"s0", "s4"},
        )
        self.assertTrue(all(_is_current(self.conn, name) for name in names))
        _assert_fts_consistent(self.conn)

    def test_missing_transcript_is_recorded_without_a_checkpoint(self):
        path = self._session("a", "alpha one", goal="kept goal")
        path.unlink()
        stats = backfill_search_index(self.conn)
        self.assertEqual(stats.missing, 1)
        self.assertEqual(_state(self.conn, "a")["transcript_status"], "missing")
        self.assertEqual(_checkpoint(self.conn, "a"), (None, None, None, None))
        self.assertTrue(search_sessions(self.conn, "kept goal"))


# Mutations applied through ordinary SQL, with every trigger intact.
MUTATIONS = st.lists(
    st.tuples(
        st.integers(0, 3),
        st.sampled_from(
            [
                ("file_hash", "f" * 64),
                ("session_goal", "changed goal"),
                ("session_summary", "changed summary"),
                ("first_user_message", "changed first message"),
                ("repo_name", "changed-repo"),
                ("project", "changed-project"),
                ("source", "codex"),
                ("visibility", "unlisted"),
                ("visibility", "public"),
                ("publication_state", "reviewed"),
                ("reviewed_sha256", "r" * 64),
                ("delete_state", None),
                ("error_state", None),
                ("delete_session", None),
                ("same_value", None),
            ]
        ),
    ),
    max_size=6,
)


class StaleCandidatePropertyTests(unittest.TestCase):
    @settings(max_examples=150, deadline=None)
    @given(mutations=MUTATIONS, extra_sessions=st.integers(0, 2))
    def test_prefilter_selects_exactly_the_stale_rows_while_triggers_hold(
        self, mutations, extra_sessions
    ):
        with tempfile.TemporaryDirectory() as td:
            conn = _connect()
            for index in range(4):
                path = Path(td) / f"s{index}.jsonl"
                path.write_bytes(_line(_user(f"text {index}")))
                _insert_session(conn, f"s{index}", path=path, goal=f"goal {index}")
                _index(conn, f"s{index}", path)
            for index, (column, value) in mutations:
                session_id = f"s{index}"
                if column == "delete_state":
                    conn.execute(
                        "DELETE FROM session_search_state WHERE session_id = ?",
                        (session_id,),
                    )
                elif column == "error_state":
                    row = search._session_row(conn, session_id)
                    if row is not None:
                        search._record_search_error(
                            conn, row, status="error", error="x"
                        )
                elif column == "delete_session":
                    conn.execute(
                        "DELETE FROM sessions WHERE session_id = ?", (session_id,)
                    )
                elif column == "same_value":
                    conn.execute(
                        "UPDATE sessions SET session_goal = session_goal "
                        "WHERE session_id = ?",
                        (session_id,),
                    )
                else:
                    conn.execute(
                        f"UPDATE sessions SET {column} = ? WHERE session_id = ?",
                        (value, session_id),
                    )
            for index in range(extra_sessions):
                _insert_session(conn, f"new{index}")

            stale = {
                row["session_id"]
                for row in conn.execute(search._BACKFILL_ROWS_SQL)
                if not search._state_is_current(row)
            }
            # Exact while the triggers hold: no stale row is missed, and no
            # current row is loaded or hashed.
            self.assertEqual(stale, set(search._stale_candidate_ids(conn)))


class CheckpointSchemaTests(unittest.TestCase):
    def test_columns_are_added_to_a_legacy_state_table_idempotently(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE session_search_state (
                session_id TEXT PRIMARY KEY, search_version INTEGER NOT NULL,
                file_hash TEXT, artifact_hash TEXT, metadata_hash TEXT NOT NULL,
                transcript_status TEXT NOT NULL, last_error TEXT,
                indexed_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO session_search_state VALUES "
            "('old', 7, 'h', NULL, 'm', 'complete', NULL, 'now')"
        )
        ensure_search_checkpoint_columns(conn)
        ensure_search_checkpoint_columns(conn)
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(session_search_state)")
        ]
        for name, _spec in SEARCH_CHECKPOINT_COLUMNS:
            self.assertEqual(columns.count(name), 1)
        self.assertEqual(
            conn.execute(
                "SELECT transcript_offset, user_chunks FROM session_search_state"
            ).fetchone(),
            (None, None),
        )

    def test_migrate_db_adds_the_columns(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            with get_db(db_path) as conn:
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(session_search_state)")
                }
                self.assertIsNone(search_checkpoint_offset(conn, "unknown"))
            for name, _spec in SEARCH_CHECKPOINT_COLUMNS:
                self.assertIn(name, columns)


if __name__ == "__main__":
    unittest.main()
