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


class SyncLimitTests(unittest.TestCase):
    def _three_sessions(self, harness: SyncHarness) -> None:
        for name in ("a", "b", "c"):
            _append(
                _claude_path(harness.home, f"session-{name}"),
                _lines(_session_records(4)),
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
