"""Incremental, bounded sync: differential and limit tests.

The central property: syncing a set of transcripts after every append
(resuming parse states and appending search documents) leaves the database
in the same logical state as one full sync of the final bytes into a fresh
database.
"""

import json
import os
import signal
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from test_incremental_parse import _serialize, claude_records, codex_records

import logpile.sync as sync_module
from logpile.diskguard import GIB, DiskGuardDecision, DiskGuardPolicy
from logpile.search import replace_session_search_index
from logpile.sync import (
    SyncLimits,
    SyncStatus,
    parse_state_dir,
    sync_sessions,
)

INCREMENTAL = SyncLimits(
    budget_seconds=None,
    disk=DiskGuardPolicy.disabled(),
    parse_state_min_bytes=0,
)
FULL = replace(INCREMENTAL, parse_state_min_bytes=1 << 62)

VOLATILE_SESSION_COLUMNS = {"synced_at", "shared_path"}


def _claude_path(home: Path, name: str) -> Path:
    return home / ".claude" / "projects" / "-Users-alice-demo" / f"{name}.jsonl"


def _codex_path(home: Path, name: str) -> Path:
    return home / ".codex" / "sessions" / "2026" / "09" / "29" / f"{name}.jsonl"


def _rows(conn, sql, params=()):
    return [tuple(row) for row in conn.execute(sql, params)]


def snapshot(db_path: Path, shared: Path) -> dict:
    """Every logical row sync derives from transcripts, without surrogate ids."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with closing(conn):
        columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info(sessions)")
            if row[1] not in VOLATILE_SESSION_COLUMNS
        ]
        sessions = []
        for row in conn.execute(
            f"SELECT {', '.join(columns)}, shared_path FROM sessions "
            "ORDER BY session_id"
        ):
            values = dict(zip([*columns, "shared_path"], tuple(row), strict=True))
            shared_path = values.pop("shared_path")
            # Relative to the harness root, with the harness's own shared-dir
            # name masked: the private archive root is named after it.
            values["shared_relpath"] = (
                os.path.relpath(shared_path, shared.parent).replace(
                    shared.name, "SHARED"
                )
                if shared_path
                else None
            )
            sessions.append(values)
        daily_columns = [
            row[1] for row in conn.execute("PRAGMA table_info(session_daily_usage)")
        ]
        claims_columns = [
            row[1] for row in conn.execute("PRAGMA table_info(message_claims)")
        ]
        paths_columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info(session_paths)")
            if row[1] != "id"
        ]
        integrity = conn.execute(
            "INSERT INTO session_search_fts(session_search_fts) "
            "VALUES('integrity-check')"
        )
        del integrity
        return {
            "sessions": sessions,
            "tool_calls": _rows(
                conn,
                "SELECT session_id, tool_name, command, timestamp, is_error "
                "FROM tool_calls ORDER BY session_id, id",
            ),
            "session_paths": sorted(
                _rows(
                    conn,
                    f"SELECT {', '.join(paths_columns)} FROM session_paths",
                ),
                key=repr,
            ),
            "daily": _rows(
                conn,
                f"SELECT {', '.join(daily_columns)} FROM session_daily_usage "
                "ORDER BY session_id, day",
            ),
            "claims": _rows(
                conn,
                f"SELECT {', '.join(claims_columns)} FROM message_claims "
                "ORDER BY claim_key, session_id",
            ),
            "search_documents": _rows(
                conn,
                "SELECT session_id, field_label, chunk_index, structured_text, "
                "transcript_text FROM session_search_documents "
                "ORDER BY session_id, field_label, chunk_index",
            ),
            "search_state": _rows(
                conn,
                "SELECT session_id, search_version, file_hash, artifact_hash, "
                "metadata_hash, transcript_status FROM session_search_state "
                "ORDER BY session_id",
            ),
            "search_hits": _rows(
                conn,
                "SELECT d.session_id, d.field_label, d.chunk_index "
                "FROM session_search_fts f "
                "JOIN session_search_documents d ON d.id = f.rowid "
                "WHERE session_search_fts MATCH 'sync OR slow OR done' "
                "ORDER BY 1, 2, 3",
            ),
            "native_queue": _rows(
                conn, "SELECT session_id FROM native_refresh_queue ORDER BY 1"
            ),
        }


class SyncHarness:
    def __init__(self, root: Path, name: str) -> None:
        self.home = root / "home"
        self.shared = root / f"shared-{name}"
        self.db = root / f"{name}.db"

    def sync(self, limits: SyncLimits = INCREMENTAL):
        return sync_sessions(
            self.shared, self.db, "alice", "machine-1", self.home, limits=limits
        )

    def snapshot(self) -> dict:
        return snapshot(self.db, self.shared)


def _append(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(data)


def _session_records(count: int, *, start: int = 0) -> list[dict]:
    records = []
    for index in range(start, start + count):
        records.append(
            {
                "type": "user",
                "timestamp": f"2026-09-29T10:{index % 60:02d}:00Z",
                "cwd": "/tmp/demo",
                "sessionId": "session-a",
                "message": {"content": f"please fix the sync step {index}"},
            }
        )
        records.append(
            {
                "type": "assistant",
                "timestamp": f"2026-09-29T10:{index % 60:02d}:30Z",
                "requestId": f"req_{index}",
                "message": {
                    "id": f"msg_{index}",
                    "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 3, "output_tokens": index + 1},
                    "content": [
                        {"type": "text", "text": f"done with step {index}"},
                        {
                            "type": "tool_use",
                            "id": f"toolu_{index}",
                            "name": "Bash",
                            "input": {"command": f"pytest tests/test_{index}.py"},
                        },
                    ],
                },
            }
        )
        records.append(
            {
                "type": "user",
                "timestamp": f"2026-09-29T10:{index % 60:02d}:40Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"toolu_{index}",
                            "is_error": index % 3 == 0,
                        }
                    ]
                },
            }
        )
    return records


def _lines(records) -> bytes:
    return "".join(json.dumps(record) + "\n" for record in records).encode()


def _without(records, predicate):
    return [record for record in records if not predicate(record)]


def _renames_session(record) -> bool:
    return bool(record.get("isSidechain") or record.get("agentId"))


def _marks_private(record) -> bool:
    return ":private" in json.dumps(record)


# Two sessions are history dependent by design, in the old code as in this
# one: a file that first becomes a sidechain part-way through is renamed
# (agent-<id>) and the earlier row is left behind, and a privacy marker
# tightens an existing row to private (keeping its last stats) but keeps a
# never-seen session out entirely. Comparing against a fresh sync of the
# final bytes therefore uses records without those; the same-schedule
# comparison below keeps them.
history_free_claude = claude_records.map(
    lambda records: _without(
        records, lambda r: _renames_session(r) or _marks_private(r)
    )
)
history_free_codex = codex_records.map(
    lambda records: _without(records, _marks_private)
)
append_schedules = st.lists(
    st.tuples(st.floats(0, 1), st.floats(0, 1)), min_size=1, max_size=4
)


def _full_search_replacement(
    conn, session_id, *, transcript_path, scan, shared_dir=None
):
    del scan
    return replace_session_search_index(
        conn, session_id, transcript_path=transcript_path, shared_dir=shared_dir
    )


def _grow_and_sync(root, claude_data, codex_data, cuts, harnesses):
    """Append each file up to every cut, syncing each harness after each step.

    ``harnesses`` pairs a SyncHarness with its limits and a flag that forces
    the reference behavior: no parse state and a full search replacement.
    Returns whether any incremental parse resumed from a checkpoint.
    """
    home = harnesses[0][0].home
    claude_path = _claude_path(home, "session-a")
    codex_path = _codex_path(home, "rollout-2026-09-29-a")
    written = [0, 0]
    resumed = False
    for claude_cut, codex_cut in [*sorted(cuts), (1.0, 1.0)]:
        for index, (path, data, fraction) in enumerate(
            (
                (claude_path, claude_data, claude_cut),
                (codex_path, codex_data, codex_cut),
            )
        ):
            target = max(written[index], int(len(data) * fraction))
            _append(path, data[written[index] : target])
            written[index] = target
        for harness, limits, reference in harnesses:
            if reference:
                with mock.patch.object(
                    sync_module,
                    "update_session_search_index",
                    side_effect=_full_search_replacement,
                ):
                    harness.sync(limits)
                continue
            with mock.patch.object(
                sync_module, "parse_transcript", wraps=sync_module.parse_transcript
            ) as spy:
                harness.sync(limits)
            resumed = resumed or any(
                call.kwargs.get("checkpoint") is not None for call in spy.call_args_list
            )
    return resumed


class IncrementalSyncDifferentialTests(unittest.TestCase):
    @settings(
        max_examples=60,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(
        claude=claude_records,
        codex=codex_records,
        trailing=st.booleans(),
        cuts=append_schedules,
    )
    def test_incremental_sync_matches_full_reprocessing_on_the_same_schedule(
        self, claude, codex, trailing, cuts
    ):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            incremental = SyncHarness(root, "incremental")
            reference = SyncHarness(root, "reference")
            resumed = _grow_and_sync(
                root,
                _serialize(claude, trailing, None),
                _serialize(codex, trailing, None),
                cuts,
                [(incremental, INCREMENTAL, False), (reference, FULL, True)],
            )
            event(f"resumed={resumed}")
            self.assertEqual(incremental.snapshot(), reference.snapshot())

    @settings(
        max_examples=60,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(
        claude=history_free_claude,
        codex=history_free_codex,
        trailing=st.booleans(),
        cuts=append_schedules,
    )
    def test_incremental_sync_matches_full_sync_of_final_bytes(
        self, claude, codex, trailing, cuts
    ):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            incremental = SyncHarness(root, "incremental")
            resumed = _grow_and_sync(
                root,
                _serialize(claude, trailing, None),
                _serialize(codex, trailing, None),
                cuts,
                [(incremental, INCREMENTAL, False)],
            )
            event(f"resumed={resumed}")
            full = SyncHarness(root, "full")
            full.sync(FULL)
            self.assertEqual(incremental.snapshot(), full.snapshot())

    def test_appends_to_large_session_resume_and_match_full_sync(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            incremental = SyncHarness(root, "incremental")
            path = _claude_path(incremental.home, "session-a")
            _append(path, _lines(_session_records(40)))
            incremental.sync()
            before = incremental.snapshot()
            self.assertEqual(len(before["tool_calls"]), 40)

            with mock.patch.object(
                sync_module, "parse_transcript", wraps=sync_module.parse_transcript
            ) as spy:
                _append(path, _lines(_session_records(5, start=40)))
                incremental.sync()
            (call,) = spy.call_args_list
            self.assertIsNotNone(call.kwargs["checkpoint"])

            full = SyncHarness(root, "full")
            full.sync(FULL)
            self.assertEqual(incremental.snapshot(), full.snapshot())

    def test_append_keeps_existing_row_ids_and_search_documents(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            harness = SyncHarness(root, "incremental")
            path = _claude_path(harness.home, "session-a")
            _append(path, _lines(_session_records(30)))
            harness.sync()
            with closing(sqlite3.connect(harness.db)) as conn:
                tool_ids = [r[0] for r in conn.execute("SELECT id FROM tool_calls")]
                doc_ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM session_search_documents "
                        "WHERE field_label LIKE 'transcript_%' ORDER BY id"
                    )
                ]
            _append(path, _lines(_session_records(2, start=30)))
            harness.sync()
            with closing(sqlite3.connect(harness.db)) as conn:
                later_tool_ids = [
                    r[0] for r in conn.execute("SELECT id FROM tool_calls ORDER BY id")
                ]
                later_doc_ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM session_search_documents "
                        "WHERE field_label LIKE 'transcript_%' ORDER BY id"
                    )
                ]
            # Rows for bytes already synced are untouched; only the append adds.
            self.assertEqual(later_tool_ids[: len(tool_ids)], tool_ids)
            self.assertEqual(len(later_tool_ids), len(tool_ids) + 2)
            self.assertEqual(later_doc_ids[: len(doc_ids)], doc_ids)

    def test_unchanged_resync_does_not_parse_or_rewrite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            harness = SyncHarness(root, "incremental")
            _append(
                _claude_path(harness.home, "session-a"),
                _lines(_session_records(10)),
            )
            harness.sync()
            before = harness.snapshot()
            with mock.patch.object(
                sync_module, "parse_transcript", wraps=sync_module.parse_transcript
            ) as spy:
                result = harness.sync()
            spy.assert_not_called()
            self.assertEqual(tuple(result), (0, 0, 1))
            self.assertEqual(harness.snapshot(), before)


class SettledStructureTests(unittest.TestCase):
    def _root_cwd_session(self, home: Path) -> Path:
        path = _claude_path(home, "probe")
        _append(
            path,
            _lines(
                [
                    {
                        "type": "user",
                        "timestamp": "2026-09-29T10:00:00Z",
                        "cwd": "/",
                        "message": {"content": "Reply with exactly: ok"},
                    },
                    {
                        "type": "assistant",
                        "timestamp": "2026-09-29T10:00:01Z",
                        "message": {
                            "id": "msg_1",
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_1",
                                    "name": "Bash",
                                    "input": {"command": "true"},
                                }
                            ],
                        },
                    },
                ]
            ),
        )
        return path

    def test_root_cwd_session_without_paths_is_not_reparsed(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "db")
            self._root_cwd_session(harness.home)
            harness.sync()
            with closing(sqlite3.connect(harness.db)) as conn:
                repo_name, paths = conn.execute(
                    "SELECT repo_name, (SELECT COUNT(*) FROM session_paths) "
                    "FROM sessions"
                ).fetchone()
            # Legitimately empty: no repo at "/", no file arguments.
            self.assertIsNone(repo_name)
            self.assertEqual(paths, 0)
            with mock.patch.object(
                sync_module, "parse_transcript", wraps=sync_module.parse_transcript
            ) as spy:
                result = harness.sync()
            spy.assert_not_called()
            self.assertEqual(tuple(result), (0, 0, 1))

    def test_legacy_unstamped_rows_are_settled_once_without_reparse(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "db")
            self._root_cwd_session(harness.home)
            harness.sync()
            with closing(sqlite3.connect(harness.db)) as conn:
                conn.execute("UPDATE sessions SET structure_version = 0")
                conn.execute(
                    "DELETE FROM logpile_meta WHERE key = 'structure_version_settled'"
                )
                conn.commit()
            with mock.patch.object(
                sync_module, "parse_transcript", wraps=sync_module.parse_transcript
            ) as spy:
                harness.sync()
            spy.assert_not_called()
            with closing(sqlite3.connect(harness.db)) as conn:
                (version,) = conn.execute(
                    "SELECT structure_version FROM sessions"
                ).fetchone()
            self.assertEqual(version, sync_module.SESSION_STRUCTURE_VERSION)

    def test_outdated_parser_version_still_forces_reparse(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "db")
            self._root_cwd_session(harness.home)
            harness.sync()
            with closing(sqlite3.connect(harness.db)) as conn:
                conn.execute("UPDATE sessions SET activity_version = 0")
                conn.commit()
            with mock.patch.object(
                sync_module, "parse_transcript", wraps=sync_module.parse_transcript
            ) as spy:
                harness.sync()
            self.assertEqual(spy.call_count, 1)
            self.assertIsNone(spy.call_args.kwargs["checkpoint"])


class DuplicateSessionCopyTests(unittest.TestCase):
    def test_copies_of_one_session_id_do_not_flip_the_row(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "dups")
            projects = harness.home / ".claude" / "projects"
            older = projects / "-Users-alice-demo" / "shared-id.jsonl"
            newer = projects / "-Users-alice-demo-worktree" / "shared-id.jsonl"
            _append(older, _lines(_session_records(3)))
            _append(newer, _lines(_session_records(5, start=50)))
            os.utime(older, (1_000_000_000, 1_000_000_000))
            harness.sync()
            with closing(sqlite3.connect(harness.db)) as conn:
                (source_path,) = conn.execute(
                    "SELECT source_path FROM sessions WHERE session_id = 'shared-id'"
                ).fetchone()
            self.assertEqual(source_path, str(newer))
            before = harness.snapshot()
            with mock.patch.object(
                sync_module, "parse_transcript", wraps=sync_module.parse_transcript
            ) as spy:
                result = harness.sync()
            spy.assert_not_called()
            self.assertEqual((result.new, result.updated), (0, 0))
            self.assertEqual(harness.snapshot(), before)

            # The copy written most recently is the one synced.
            _append(older, _lines(_session_records(1, start=3)))
            harness.sync()
            with closing(sqlite3.connect(harness.db)) as conn:
                (source_path,) = conn.execute(
                    "SELECT source_path FROM sessions WHERE session_id = 'shared-id'"
                ).fetchone()
            self.assertEqual(source_path, str(older))


class DuplicateCopyMarkerTests(unittest.TestCase):
    def _copies(self, harness):
        projects = harness.home / ".claude" / "projects"
        marked = projects / "-Users-alice-demo" / "shared-id.jsonl"
        newer = projects / "-Users-alice-demo-worktree" / "shared-id.jsonl"
        marker = "logpile" + ":private"
        _append(
            marked,
            _lines(
                [
                    {
                        "type": "user",
                        "timestamp": "2026-09-29T09:00:00Z",
                        "cwd": "/tmp/demo",
                        "message": {"content": f"keep this {marker}"},
                    }
                ]
            ),
        )
        _append(newer, _lines(_session_records(3, start=50)))
        os.utime(marked, (1_000_000_000, 1_000_000_000))
        return marked, newer

    def test_a_marker_in_an_older_copy_keeps_the_session_out(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "dups")
            self._copies(harness)
            harness.sync()
            with closing(sqlite3.connect(harness.db)) as conn:
                rows = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            # A never-seen marker session is kept out entirely, as before.
            self.assertEqual(rows, 0)
            self.assertFalse(
                any(harness.shared.rglob("*.jsonl"))
                if harness.shared.exists()
                else False
            )

    def test_a_marker_in_an_older_copy_tightens_an_existing_row(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "dups")
            projects = harness.home / ".claude" / "projects"
            newer = projects / "-Users-alice-demo-worktree" / "shared-id.jsonl"
            _append(newer, _lines(_session_records(3, start=50)))
            harness.sync()
            marked, _ = self._copies(harness)
            harness.sync()
            with closing(sqlite3.connect(harness.db)) as conn:
                visibility, source_path = conn.execute(
                    "SELECT visibility, source_path FROM sessions"
                ).fetchone()
            self.assertEqual(visibility, "private")
            self.assertEqual(source_path, str(marked))

    def test_unchanged_copies_are_not_rescanned_for_markers(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "dups")
            self._copies(harness)
            harness.sync()
            with mock.patch.object(
                sync_module,
                "find_private_marker",
                side_effect=AssertionError("rescanned"),
            ):
                harness.sync()


class SyncLimitTests(unittest.TestCase):
    def _three_sessions(self, harness: SyncHarness) -> None:
        # Distinct message ids, so no claims (and no native refreshes) are shared.
        for name, start in (("a", 0), ("b", 100), ("c", 200)):
            _append(
                _claude_path(harness.home, f"session-{name}"),
                _lines(_session_records(4, start=start)),
            )

    def test_exhausted_budget_stops_after_one_session_and_converges(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            harness = SyncHarness(root, "budget")
            self._three_sessions(harness)
            tiny = replace(INCREMENTAL, budget_seconds=1e-9)
            statuses = []
            for _ in range(10):
                result = harness.sync(tiny)
                statuses.append(result.status)
                if result.status == SyncStatus.COMPLETED:
                    break
            self.assertEqual(statuses[0], SyncStatus.PARTIAL)
            self.assertIn("budget", harness.sync(tiny).reason or "budget")
            self.assertEqual(statuses[-1], SyncStatus.COMPLETED)
            full = SyncHarness(root, "full")
            full.sync(FULL)
            self.assertEqual(harness.snapshot(), full.snapshot())

    def test_budget_cut_puts_the_other_pass_first_next_run(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "budget")
            self._three_sessions(harness)
            harness.sync(replace(INCREMENTAL, budget_seconds=1e-9))
            with closing(sqlite3.connect(harness.db)) as conn:
                (first,) = conn.execute(
                    "SELECT value FROM logpile_meta WHERE key = 'sync_first_pass'"
                ).fetchone()
            self.assertEqual(first, "codex")

    def test_disk_guard_defers_before_writing_anything(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "disk")
            self._three_sessions(harness)
            guarded = replace(INCREMENTAL, disk=DiskGuardPolicy.from_gib(40, 60))
            low = DiskGuardDecision(
                ok=False,
                path=harness.db,
                free_bytes=16 * GIB,
                floor_bytes=60 * GIB,
                snapshot_count=1,
                reason="16.0 GiB free is below the 60.0 GiB floor",
            )
            with (
                mock.patch.object(sync_module, "check_disk_space", return_value=low),
                mock.patch.object(
                    sync_module, "list_local_snapshots", return_value=("s",)
                ),
            ):
                result = harness.sync(guarded)
            self.assertEqual(result.status, SyncStatus.DISK_DEFERRED)
            self.assertIn("16.0 GiB", result.reason)
            self.assertFalse(harness.db.exists())
            self.assertFalse(harness.shared.exists())

    def test_disk_guard_stops_a_running_sync_at_a_session_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "disk")
            self._three_sessions(harness)
            guarded = replace(
                INCREMENTAL,
                disk=DiskGuardPolicy.from_gib(40, 60),
                disk_recheck_seconds=0.0,
            )
            ok = DiskGuardDecision(True, harness.db, 100 * GIB, 40 * GIB, 0, "ok")
            low = DiskGuardDecision(False, harness.db, 1 * GIB, 40 * GIB, 0, "low")
            decisions = iter([ok, ok, low])
            with (
                mock.patch.object(
                    sync_module,
                    "check_disk_space",
                    side_effect=lambda *a, **k: next(decisions, low),
                ),
                mock.patch.object(sync_module, "list_local_snapshots", return_value=()),
            ):
                result = harness.sync(guarded)
            self.assertEqual(result.status, SyncStatus.PARTIAL)
            self.assertEqual(result.reason, "low")
            self.assertEqual(result.new, 1)

    def test_sigterm_stops_cleanly_and_the_next_run_finishes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            harness = SyncHarness(root, "term")
            self._three_sessions(harness)
            real_parse = sync_module.parse_transcript
            calls = []

            def parse_then_signal(*args, **kwargs):
                calls.append(args)
                if len(calls) == 1:
                    os.kill(os.getpid(), signal.SIGTERM)
                return real_parse(*args, **kwargs)

            previous = signal.getsignal(signal.SIGTERM)
            with mock.patch.object(
                sync_module, "parse_transcript", side_effect=parse_then_signal
            ):
                result = harness.sync()
            self.assertIs(signal.getsignal(signal.SIGTERM), previous)
            self.assertEqual(result.status, SyncStatus.PARTIAL)
            self.assertEqual(result.reason, "received SIGTERM")
            self.assertEqual(result.new, 1)
            self.assertEqual(harness.sync().status, SyncStatus.COMPLETED)
            full = SyncHarness(root, "full")
            full.sync(FULL)
            self.assertEqual(harness.snapshot(), full.snapshot())

    def test_stop_signal_skips_end_of_run_backfills(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "tail")
            self._three_sessions(harness)
            harness.sync()
            with closing(sqlite3.connect(harness.db)) as conn:
                # Owed work from an earlier interrupted run.
                conn.execute(
                    "INSERT INTO native_refresh_queue (session_id) "
                    "SELECT session_id FROM sessions"
                )
                conn.execute(
                    "UPDATE session_search_state SET transcript_status = 'stale'"
                )
                conn.commit()
            _append(
                _claude_path(harness.home, "session-a"),
                _lines(_session_records(1, start=4)),
            )
            real_parse = sync_module.parse_transcript

            def parse_then_signal(*args, **kwargs):
                os.kill(os.getpid(), signal.SIGTERM)
                return real_parse(*args, **kwargs)

            with (
                mock.patch.object(
                    sync_module, "parse_transcript", side_effect=parse_then_signal
                ),
                mock.patch.object(
                    sync_module,
                    "drain_native_refresh",
                    wraps=sync_module.drain_native_refresh,
                ) as drain,
            ):
                result = harness.sync()
            self.assertEqual(result.status, SyncStatus.PARTIAL)
            self.assertEqual(result.reason, "received SIGTERM")
            self.assertTrue(drain.call_args.kwargs["should_stop"]())
            with closing(sqlite3.connect(harness.db)) as conn:
                queued = conn.execute(
                    "SELECT COUNT(*) FROM native_refresh_queue"
                ).fetchone()[0]
                stale = conn.execute(
                    "SELECT COUNT(*) FROM session_search_state "
                    "WHERE transcript_status = 'stale'"
                ).fetchone()[0]
            # This run's own session was refreshed; the rest stays owed.
            self.assertEqual(queued, 2)
            self.assertEqual(stale, 2)
            self.assertEqual(harness.sync().status, SyncStatus.COMPLETED)

    def test_disk_stop_skips_end_of_run_backfills(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "tail-disk")
            self._three_sessions(harness)
            harness.sync()
            with closing(sqlite3.connect(harness.db)) as conn:
                conn.execute(
                    "INSERT INTO native_refresh_queue (session_id) "
                    "SELECT session_id FROM sessions"
                )
                conn.execute(
                    "UPDATE session_search_state SET transcript_status = 'stale'"
                )
                conn.commit()
            guarded = replace(
                INCREMENTAL,
                disk=DiskGuardPolicy.from_gib(40, 60),
                disk_recheck_seconds=0.0,
            )
            ok = DiskGuardDecision(True, harness.db, 100 * GIB, 40 * GIB, 0, "ok")
            low = DiskGuardDecision(False, harness.db, 1 * GIB, 40 * GIB, 0, "low")
            # Fine at the start and through the (unchanged) passes, low after.
            decisions = iter([ok, ok, ok, ok])
            with (
                mock.patch.object(
                    sync_module,
                    "check_disk_space",
                    side_effect=lambda *a, **k: next(decisions, low),
                ),
                mock.patch.object(sync_module, "list_local_snapshots", return_value=()),
            ):
                result = harness.sync(guarded)
            self.assertEqual(result.status, SyncStatus.PARTIAL)
            self.assertEqual(result.reason, "low")
            with closing(sqlite3.connect(harness.db)) as conn:
                queued = conn.execute(
                    "SELECT COUNT(*) FROM native_refresh_queue"
                ).fetchone()[0]
                stale = conn.execute(
                    "SELECT COUNT(*) FROM session_search_state "
                    "WHERE transcript_status = 'stale'"
                ).fetchone()[0]
            self.assertEqual((queued, stale), (3, 3))

    def test_budget_stop_still_lets_search_backfill_make_progress(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "tail-budget")
            self._three_sessions(harness)
            harness.sync()
            with closing(sqlite3.connect(harness.db)) as conn:
                conn.execute(
                    "UPDATE session_search_state SET transcript_status = 'stale' "
                    "WHERE session_id != 'session-a'"
                )
                conn.commit()
            _append(
                _claude_path(harness.home, "session-a"),
                _lines(_session_records(1, start=4)),
            )
            result = harness.sync(replace(INCREMENTAL, budget_seconds=1e-9))
            self.assertEqual(result.status, SyncStatus.PARTIAL)
            with closing(sqlite3.connect(harness.db)) as conn:
                stale = conn.execute(
                    "SELECT COUNT(*) FROM session_search_state "
                    "WHERE transcript_status = 'stale'"
                ).fetchone()[0]
            # One transcript of backfill progress, even past the deadline.
            self.assertEqual(stale, 1)

    def test_second_sigterm_restores_the_default_disposition(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "term2")
            self._three_sessions(harness)
            seen = []
            real_parse = sync_module.parse_transcript

            def parse_then_signal(*args, **kwargs):
                if not seen:
                    os.kill(os.getpid(), signal.SIGTERM)
                    seen.append(signal.getsignal(signal.SIGTERM))
                return real_parse(*args, **kwargs)

            previous = signal.getsignal(signal.SIGTERM)
            with mock.patch.object(
                sync_module, "parse_transcript", side_effect=parse_then_signal
            ):
                harness.sync()
            # After the first SIGTERM the handler is gone: a second one gets
            # whatever disposition the process had before sync started.
            self.assertEqual(seen, [previous])

    def test_cli_budget_accepts_off_and_rejects_nonsense(self):
        from click.testing import CliRunner

        from logpile.cli import cli
        from logpile.sync import SyncResult

        with tempfile.TemporaryDirectory() as td:
            args = [
                "sync",
                "--db",
                str(Path(td) / "x.db"),
                "--shared",
                str(Path(td) / "shared"),
                "--username",
                "alice",
            ]
            for raw, expected in (("off", None), ("0", None), ("120", 120.0)):
                with mock.patch(
                    "logpile.sync.sync_sessions", return_value=SyncResult(0, 0, 0)
                ) as sync:
                    result = CliRunner().invoke(
                        cli, args, env={"LOGPILE_SYNC_BUDGET_SECONDS": raw}
                    )
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(
                    sync.call_args.kwargs["limits"].budget_seconds, expected
                )
            for bad in ("-1", "nan"):
                result = CliRunner().invoke(cli, [*args, "--budget", bad])
                self.assertEqual(result.exit_code, 2, bad)
            with mock.patch(
                "logpile.sync.sync_sessions", return_value=SyncResult(0, 0, 0)
            ) as sync:
                result = CliRunner().invoke(cli, [*args, "--budget", "inf"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIsNone(sync.call_args.kwargs["limits"].budget_seconds)

    def test_limits_from_env(self):
        limits = SyncLimits.from_env(
            {
                "LOGPILE_SYNC_BUDGET_SECONDS": "120",
                "LOGPILE_SYNC_MIN_FREE_GIB": "10",
                "LOGPILE_SYNC_MIN_FREE_GIB_WITH_SNAPSHOT": "20",
                "LOGPILE_PARSE_STATE_MIN_BYTES": "0",
            }
        )
        self.assertEqual(limits.budget_seconds, 120)
        self.assertEqual(limits.disk.min_free_bytes, 10 * GIB)
        self.assertEqual(limits.disk.min_free_with_snapshot_bytes, 20 * GIB)
        self.assertEqual(limits.parse_state_min_bytes, 0)
        self.assertTrue(limits.disk.enabled)
        unlimited = SyncLimits.from_env(
            {"LOGPILE_SYNC_BUDGET_SECONDS": "0", "LOGPILE_SYNC_DISK_GUARD": "off"}
        )
        self.assertIsNone(unlimited.budget_seconds)
        self.assertFalse(unlimited.disk.enabled)
        default = SyncLimits.from_env({})
        self.assertEqual(default.budget_seconds, 900)
        self.assertEqual(default.disk.min_free_bytes, 40 * GIB)


class ArchivalCopyRaceTests(unittest.TestCase):
    def test_source_growing_after_scan_still_archives_the_scanned_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            harness = SyncHarness(root, "race")
            path = _claude_path(harness.home, "session-a")
            _append(path, _lines(_session_records(6)))
            real_scan = sync_module._scan_for_sync
            scanned = {}

            def scan_then_grow(conn, jsonl_path, session_id, *, resume):
                transcript = real_scan(conn, jsonl_path, session_id, resume=resume)
                scanned["bytes"] = jsonl_path.read_bytes()[: transcript.scan.size]
                _append(jsonl_path, _lines(_session_records(1, start=6)))
                return transcript

            with mock.patch.object(
                sync_module, "_scan_for_sync", side_effect=scan_then_grow
            ):
                result = harness.sync()
            self.assertEqual(result.new, 1)
            with closing(sqlite3.connect(harness.db)) as conn:
                shared_path, size, retries = conn.execute(
                    "SELECT shared_path, file_size, "
                    "(SELECT COUNT(*) FROM sync_copy_retries) FROM sessions"
                ).fetchone()
            self.assertEqual(retries, 0)
            self.assertEqual(Path(shared_path).read_bytes(), scanned["bytes"])
            self.assertEqual(size, len(scanned["bytes"]))
            # The bytes appended after the scan are picked up next time.
            harness.sync()
            full = SyncHarness(root, "full")
            full.sync(FULL)
            self.assertEqual(harness.snapshot(), full.snapshot())


class ParseStateCollectionTests(unittest.TestCase):
    def test_marker_private_session_keeps_no_parse_state(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "marker")
            path = _claude_path(harness.home, "session-a")
            _append(path, _lines(_session_records(4)))
            harness.sync()
            state_dir = parse_state_dir(harness.db)
            self.assertEqual(len(list(state_dir.iterdir())), 1)
            marker = "logpile" + ":private"
            _append(
                path,
                _lines(
                    [
                        {
                            "type": "user",
                            "timestamp": "2026-09-29T11:00:00Z",
                            "message": {"content": f"keep this {marker}"},
                        }
                    ]
                ),
            )
            harness.sync()
            self.assertEqual(list(state_dir.iterdir()), [])
            with closing(sqlite3.connect(harness.db)) as conn:
                visibility, generation = conn.execute(
                    "SELECT visibility, "
                    "(SELECT parse_generation FROM transcript_checkpoints) "
                    "FROM sessions"
                ).fetchone()
            self.assertEqual(visibility, "private")
            # Only where the marker lies is kept, not any derived content.
            self.assertEqual(generation, "marker:" + marker)

            # While those bytes are unchanged, appends are never parsed.
            _append(path, _lines(_session_records(2, start=10)))
            import logpile.parsers as parsers_module

            with mock.patch.object(
                parsers_module._ClaudeStream,
                "feed",
                side_effect=AssertionError("parsed"),
            ):
                harness.sync()
            self.assertEqual(list(state_dir.iterdir()), [])

    def test_damaged_state_file_falls_back_to_a_full_parse(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            harness = SyncHarness(root, "damaged")
            path = _claude_path(harness.home, "session-a")
            _append(path, _lines(_session_records(6)))
            harness.sync()
            (state,) = parse_state_dir(harness.db).iterdir()
            state.write_bytes(b"not a sqlite database" * 100)
            _append(path, _lines(_session_records(2, start=6)))
            self.assertEqual(harness.sync().status, SyncStatus.COMPLETED)
            full = SyncHarness(root, "full")
            full.sync(FULL)
            self.assertEqual(harness.snapshot(), full.snapshot())

    def test_idle_states_and_orphans_are_removed(self):
        with tempfile.TemporaryDirectory() as td:
            harness = SyncHarness(Path(td), "gc")
            path = _claude_path(harness.home, "session-a")
            _append(path, _lines(_session_records(4)))
            harness.sync()
            state_dir = parse_state_dir(harness.db)
            states = sorted(p.name for p in state_dir.iterdir())
            self.assertEqual(len(states), 1)
            orphan = state_dir / ("0" * 32 + ".sqlite")
            orphan.write_bytes(b"")
            harness.sync()
            self.assertEqual(sorted(p.name for p in state_dir.iterdir()), states)

            old = path.stat().st_mtime - 10 * 24 * 3600
            os.utime(path, (old, old))
            with closing(sqlite3.connect(harness.db)) as conn:
                conn.execute("UPDATE transcript_checkpoints SET file_mtime = ?", (old,))
                conn.commit()
            harness.sync()
            self.assertEqual(list(state_dir.iterdir()), [])
            with closing(sqlite3.connect(harness.db)) as conn:
                (count,) = conn.execute(
                    "SELECT COUNT(*) FROM transcript_checkpoints"
                ).fetchone()
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
