import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from logpile.cli import cli
from logpile.db import get_db, init_db
from logpile.subfleet import (
    SubfleetSpoolError,
    get_task_timeline,
    ingest_subfleet_events,
    list_tasks,
)
from logpile.sync import sync_sessions

CLAUDE_SESSION = "9e2fe992-bddf-49f6-8da4-d90e68139dc5"
CLAUDE_SESSION_2 = "fd4f82dd-c197-46ee-9f11-d81633f98d3f"
CODEX_THREAD = "01a03958-91a2-7682-a883-d08e638b0192"


def _ref(kind: str, number: int) -> str:
    return f"{kind}_{number:024x}"


def _base_event(
    event_type: str,
    *,
    event_number: int,
    run_number: int,
    provider: str = "claude",
    task_number: int = 1,
    occurred_at: str = "2026-08-28T10:00:00Z",
) -> dict:
    return {
        "schema_version": 1,
        "event": event_type,
        "event_id": _ref("event", event_number),
        "task_id": _ref("task", task_number),
        "run_id": _ref("run", run_number),
        "provider": provider,
        "lane_ref": _ref("lane", 1),
        "workspace": {"ref": _ref("workspace", 1)},
        "timestamps": {"occurred_at": occurred_at},
    }


def _started(**kwargs) -> dict:
    event = _base_event("run.started", **kwargs)
    event["timestamps"]["started_at"] = event["timestamps"]["occurred_at"]
    return event


def _bound(
    *,
    attempt_number: int,
    native_id: str,
    **kwargs,
) -> dict:
    event = _base_event("run.bound", **kwargs)
    event["attempt_id"] = _ref("attempt", attempt_number)
    event["binding"] = {
        "kind": "session" if event["provider"] == "claude" else "thread",
        "native_id": native_id,
    }
    return event


def _handoff(
    *,
    source_attempt_number: int,
    source_native_id: str,
    source_provider: str = "claude",
    **kwargs,
) -> dict:
    event = _base_event("handoff.created", **kwargs)
    event["source"] = {
        "provider": source_provider,
        "attempt_id": _ref("attempt", source_attempt_number),
        "binding": {
            "kind": "session" if source_provider == "claude" else "thread",
            "native_id": source_native_id,
        },
    }
    return event


def _finished(
    *,
    attempt_number: int | None,
    exit_code: int = 0,
    **kwargs,
) -> dict:
    event = _base_event("run.finished", **kwargs)
    if attempt_number is not None:
        event["attempt_id"] = _ref("attempt", attempt_number)
    timestamp = event["timestamps"]["occurred_at"]
    event["timestamps"].update({"started_at": timestamp, "finished_at": timestamp})
    event["outcome"] = {
        "status": "succeeded" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "duration_s": 0.0,
    }
    return event


def _spool(root: Path) -> Path:
    path = root / "state" / "integration-events" / "v1"
    path.mkdir(parents=True)
    return path


def _write_events(spool: Path, run_number: int, events: list[dict]) -> Path:
    path = spool / f"{_ref('run', run_number)}.jsonl"
    path.write_text(
        "".join(f"{json.dumps(event)}\n" for event in events),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _insert_session(
    conn,
    *,
    session_id: str,
    source: str,
    thread_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sessions (
            session_id, source, username, project, source_path, shared_path,
            first_timestamp, last_timestamp, thread_id, session_status
        ) VALUES (?, ?, 'alice', 'demo', ?, ?, ?, ?, ?, 'completed')
        """,
        (
            session_id,
            source,
            f"/private/{session_id}.jsonl",
            f"/archive/{session_id}.jsonl",
            "2026-08-28T10:00:00+00:00",
            "2026-08-28T10:00:01+00:00",
            thread_id,
        ),
    )


class SubfleetEventTests(unittest.TestCase):
    def test_explicit_handoff_joins_source_and_target_without_heuristics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            # The target file sorts first, so the edge itself must establish
            # the source binding before its lifecycle record is ingested.
            _write_events(
                spool,
                1,
                [
                    _started(
                        event_number=3,
                        run_number=1,
                        provider="codex",
                        occurred_at="2026-08-28T10:00:00Z",
                    ),
                    _handoff(
                        event_number=4,
                        run_number=1,
                        provider="codex",
                        source_attempt_number=1,
                        source_native_id=CLAUDE_SESSION,
                        occurred_at="2026-08-28T10:00:00Z",
                    ),
                    _bound(
                        event_number=5,
                        run_number=1,
                        provider="codex",
                        attempt_number=2,
                        native_id=CODEX_THREAD,
                        occurred_at="2026-08-28T10:00:00Z",
                    ),
                ],
            )
            _write_events(
                spool,
                2,
                [
                    _started(
                        event_number=1,
                        run_number=2,
                        occurred_at="2026-08-28T09:59:00Z",
                    ),
                    _bound(
                        event_number=2,
                        run_number=2,
                        attempt_number=1,
                        native_id=CLAUDE_SESSION,
                        occurred_at="2026-08-28T09:59:00Z",
                    ),
                ],
            )
            # Same workspace and timestamp are deliberately insufficient to
            # attach an unrelated task to the explicit edge.
            _write_events(
                spool,
                3,
                [
                    _started(
                        event_number=6,
                        run_number=3,
                        task_number=2,
                        occurred_at="2026-08-28T10:00:00Z",
                    )
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                _insert_session(
                    conn,
                    session_id=CLAUDE_SESSION,
                    source="claudecode",
                )
                _insert_session(
                    conn,
                    session_id="rollout-codex",
                    source="codex",
                    thread_id=CODEX_THREAD,
                )
                result = ingest_subfleet_events(conn, spool)
                timeline = get_task_timeline(conn, _ref("task", 1))
                unrelated = get_task_timeline(conn, _ref("task", 2))
                tasks = {task["task_id"]: task for task in list_tasks(conn)}

            timeline_json = CliRunner().invoke(
                cli,
                [
                    "task-timeline",
                    _ref("task", 1),
                    "--db",
                    str(db_path),
                    "--json",
                ],
            )
            timeline_human = CliRunner().invoke(
                cli,
                ["task-timeline", _ref("task", 1), "--db", str(db_path)],
            )
            task_list_human = CliRunner().invoke(
                cli,
                ["task-list", "--db", str(db_path)],
            )

            self.assertEqual((result.inserted, result.reconciled), (6, 2))
            self.assertEqual(
                [event["event_type"] for event in timeline],
                [
                    "run.started",
                    "run.bound",
                    "run.started",
                    "handoff.created",
                    "run.bound",
                ],
            )
            edge = next(
                event for event in timeline if event["event_type"] == "handoff.created"
            )
            self.assertEqual(edge["provider"], "codex")
            self.assertEqual(edge["source_provider"], "claude")
            self.assertEqual(edge["source_attempt_id"], _ref("attempt", 1))
            self.assertEqual(edge["source_native_id"], CLAUDE_SESSION)
            self.assertEqual(edge["source_session_id"], CLAUDE_SESSION)
            self.assertIsNone(edge["session_id"])
            self.assertEqual(timeline[-1]["session_id"], "rollout-codex")
            self.assertEqual(tasks[_ref("task", 1)]["providers"], ["claude", "codex"])
            self.assertEqual(
                (
                    tasks[_ref("task", 1)]["run_count"],
                    tasks[_ref("task", 1)]["attempt_count"],
                    tasks[_ref("task", 1)]["handoff_count"],
                ),
                (2, 2, 1),
            )
            self.assertEqual(
                [event["task_id"] for event in unrelated], [_ref("task", 2)]
            )
            self.assertEqual(tasks[_ref("task", 2)]["handoff_count"], 0)
            self.assertEqual(timeline_json.exit_code, 0, timeline_json.output)
            json_edge = next(
                event
                for event in json.loads(timeline_json.output)["events"]
                if event["event_type"] == "handoff.created"
            )
            self.assertEqual(json_edge["source_session_id"], CLAUDE_SESSION)
            self.assertEqual(timeline_human.exit_code, 0, timeline_human.output)
            self.assertIn(
                f"claude:{CLAUDE_SESSION} -> codex",
                timeline_human.output,
            )
            self.assertEqual(task_list_human.exit_code, 0, task_list_human.output)
            self.assertIn("handoffs=1", task_list_human.output)

    def test_handoff_replays_and_relational_conflicts_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            path = _write_events(
                spool,
                1,
                [
                    _started(event_number=1, run_number=1, provider="codex"),
                    _handoff(
                        event_number=2,
                        run_number=1,
                        provider="codex",
                        source_attempt_number=1,
                        source_native_id=CLAUDE_SESSION,
                    ),
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                first = ingest_subfleet_events(conn, spool)
                replay = ingest_subfleet_events(conn, spool)

            changed = _handoff(
                event_number=2,
                run_number=1,
                provider="codex",
                source_attempt_number=1,
                source_native_id=CLAUDE_SESSION,
            )
            changed["lane_ref"] = _ref("lane", 99)
            second_edge = _handoff(
                event_number=3,
                run_number=1,
                provider="codex",
                source_attempt_number=1,
                source_native_id=CLAUDE_SESSION,
            )
            global_id_collision = _handoff(
                event_number=1,
                run_number=1,
                provider="codex",
                source_attempt_number=1,
                source_native_id=CLAUDE_SESSION,
            )
            path.write_text(
                "".join(
                    f"{json.dumps(event)}\n"
                    for event in (changed, second_edge, global_id_collision)
                ),
                encoding="utf-8",
            )
            path.chmod(0o600)
            _write_events(
                spool,
                2,
                [
                    _bound(
                        event_number=4,
                        run_number=2,
                        task_number=2,
                        attempt_number=1,
                        native_id=CLAUDE_SESSION,
                    )
                ],
            )
            with get_db(db_path) as conn:
                second = ingest_subfleet_events(conn, spool)
                handoffs = conn.execute(
                    "SELECT event_id, lane_ref FROM subfleet_handoffs"
                ).fetchall()
                attempts = conn.execute(
                    "SELECT attempt_id, provider, native_id FROM subfleet_attempts"
                ).fetchall()

            self.assertEqual(first.inserted, 2)
            self.assertEqual(
                (replay.inserted, replay.duplicates, replay.rejected),
                (0, 2, 0),
            )
            self.assertEqual((second.inserted, second.rejected), (0, 4))
            self.assertEqual(
                [(row["event_id"], row["lane_ref"]) for row in handoffs],
                [(_ref("event", 2), _ref("lane", 1))],
            )
            self.assertEqual(
                [tuple(row) for row in attempts],
                [(_ref("attempt", 1), "claude", CLAUDE_SESSION)],
            )

    def test_exact_pre_handoff_multitask_attempt_replays_after_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            first = _bound(
                event_number=1,
                run_number=1,
                task_number=1,
                attempt_number=1,
                native_id=CLAUDE_SESSION,
            )
            second = _bound(
                event_number=2,
                run_number=2,
                task_number=2,
                attempt_number=1,
                native_id=CLAUDE_SESSION,
            )
            _write_events(spool, 1, [first])
            _write_events(spool, 2, [second])

            init_db(db_path)
            with get_db(db_path) as conn:
                # These table shapes and rows reproduce committed HEAD: it had
                # no handoff table and accepted an exact native attempt in two
                # tasks as long as provider and binding identity agreed.
                conn.execute("DROP VIEW subfleet_task_timeline")
                conn.execute("DROP VIEW subfleet_task_catalog")
                conn.execute("DROP TABLE subfleet_handoffs")
                conn.execute(
                    """
                    INSERT INTO subfleet_attempts (
                        attempt_id, provider, binding_kind, native_id,
                        first_bound_at
                    ) VALUES (?, 'claude', 'session', ?, ?)
                    """,
                    (
                        _ref("attempt", 1),
                        CLAUDE_SESSION,
                        "2026-08-28T10:00:00+00:00",
                    ),
                )
                for event in (first, second):
                    conn.execute(
                        """
                        INSERT INTO subfleet_events (
                            event_id, schema_version, event_type, task_id,
                            run_id, attempt_id, provider, lane_ref,
                            workspace_ref, occurred_at, binding_kind,
                            binding_native_id, ingested_at
                        ) VALUES (?, 1, 'run.bound', ?, ?, ?, 'claude', ?, ?,
                                  ?, 'session', ?, ?)
                        """,
                        (
                            event["event_id"],
                            event["task_id"],
                            event["run_id"],
                            event["attempt_id"],
                            event["lane_ref"],
                            event["workspace"]["ref"],
                            "2026-08-28T10:00:00+00:00",
                            CLAUDE_SESSION,
                            "2026-08-28T10:00:00+00:00",
                        ),
                    )

            init_db(db_path)
            with get_db(db_path) as conn:
                replay = ingest_subfleet_events(conn, spool)

            self.assertEqual(
                (replay.inserted, replay.duplicates, replay.rejected),
                (0, 2, 0),
            )

            # A new conflicting event still fails the post-upgrade invariant;
            # only the exact event-id/value rows receive compatibility handling.
            _write_events(
                spool,
                3,
                [
                    _bound(
                        event_number=3,
                        run_number=3,
                        task_number=3,
                        attempt_number=1,
                        native_id=CLAUDE_SESSION,
                    )
                ],
            )
            with get_db(db_path) as conn:
                next_sync = ingest_subfleet_events(conn, spool)
                counts = (
                    conn.execute("SELECT COUNT(*) FROM subfleet_events").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM subfleet_attempts").fetchone()[
                        0
                    ],
                )

            self.assertEqual(
                (next_sync.inserted, next_sync.duplicates, next_sync.rejected),
                (0, 2, 1),
            )
            self.assertEqual(counts, (2, 1))

    def test_handoff_cannot_target_its_exact_source_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            _write_events(
                spool,
                1,
                [
                    _started(event_number=1, run_number=1),
                    _handoff(
                        event_number=2,
                        run_number=1,
                        source_attempt_number=1,
                        source_native_id=CLAUDE_SESSION,
                    ),
                    _bound(
                        event_number=3,
                        run_number=1,
                        attempt_number=1,
                        native_id=CLAUDE_SESSION,
                    ),
                ],
            )
            _write_events(
                spool,
                2,
                [
                    _started(
                        event_number=4,
                        run_number=2,
                        task_number=2,
                    ),
                    _bound(
                        event_number=5,
                        run_number=2,
                        task_number=2,
                        attempt_number=2,
                        native_id=CLAUDE_SESSION_2,
                    ),
                    _handoff(
                        event_number=6,
                        run_number=2,
                        task_number=2,
                        source_attempt_number=2,
                        source_native_id=CLAUDE_SESSION_2,
                    ),
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                result = ingest_subfleet_events(conn, spool)
                event_types = [
                    tuple(row)
                    for row in conn.execute(
                        """
                        SELECT task_id, event_type
                        FROM subfleet_task_timeline
                        ORDER BY task_id, event_type
                        """
                    ).fetchall()
                ]

            self.assertEqual((result.inserted, result.rejected), (4, 2))
            self.assertEqual(
                event_types,
                [
                    (_ref("task", 1), "handoff.created"),
                    (_ref("task", 1), "run.started"),
                    (_ref("task", 2), "run.bound"),
                    (_ref("task", 2), "run.started"),
                ],
            )

    def test_ingests_idempotently_and_joins_both_providers_in_stable_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            init_db(db_path)
            _write_events(
                spool,
                1,
                [
                    _finished(event_number=3, run_number=1, attempt_number=1),
                    _bound(
                        event_number=2,
                        run_number=1,
                        attempt_number=1,
                        native_id=CLAUDE_SESSION,
                    ),
                    _started(event_number=1, run_number=1),
                ],
            )
            _write_events(
                spool,
                2,
                [
                    _finished(
                        event_number=6,
                        run_number=2,
                        attempt_number=2,
                        provider="codex",
                        exit_code=7,
                    ),
                    _bound(
                        event_number=5,
                        run_number=2,
                        attempt_number=2,
                        native_id=CODEX_THREAD,
                        provider="codex",
                    ),
                    _started(event_number=4, run_number=2, provider="codex"),
                ],
            )
            _write_events(
                spool,
                3,
                [
                    _started(
                        event_number=7,
                        run_number=3,
                        task_number=2,
                        occurred_at="2026-08-28T11:00:00Z",
                    )
                ],
            )

            with get_db(db_path) as conn:
                _insert_session(
                    conn,
                    session_id=CLAUDE_SESSION,
                    source="claudecode",
                )
                _insert_session(
                    conn,
                    session_id="rollout-codex",
                    source="codex",
                    thread_id=CODEX_THREAD,
                )
                result = ingest_subfleet_events(
                    conn, spool, ingested_at="2026-08-28T11:00:00+00:00"
                )
                timeline = get_task_timeline(conn, _ref("task", 1))
                tasks = list_tasks(conn)
                limited_tasks = list_tasks(conn, limit=1)
                with self.assertRaises(ValueError):
                    list_tasks(conn, limit=0)
                replay = ingest_subfleet_events(
                    conn, spool, ingested_at="2026-08-28T12:00:00+00:00"
                )

            self.assertEqual((result.inserted, result.reconciled), (7, 2))
            self.assertEqual(
                (replay.inserted, replay.duplicates, replay.rejected),
                (0, 7, 0),
            )
            self.assertEqual(
                [event["event_id"] for event in timeline],
                [_ref("event", number) for number in (1, 4, 2, 5, 3, 6)],
            )
            by_event = {event["event_id"]: event for event in timeline}
            self.assertIsNone(by_event[_ref("event", 1)]["session_id"])
            self.assertEqual(by_event[_ref("event", 2)]["session_id"], CLAUDE_SESSION)
            self.assertEqual(by_event[_ref("event", 3)]["session_id"], CLAUDE_SESSION)
            self.assertEqual(by_event[_ref("event", 5)]["session_id"], "rollout-codex")
            self.assertEqual(by_event[_ref("event", 6)]["native_id"], CODEX_THREAD)
            self.assertEqual(
                [task["task_id"] for task in tasks],
                [_ref("task", 2), _ref("task", 1)],
            )
            self.assertEqual(limited_tasks[0]["task_id"], _ref("task", 2))
            task = next(task for task in tasks if task["task_id"] == _ref("task", 1))
            self.assertEqual(task["providers"], ["claude", "codex"])
            self.assertEqual((task["run_count"], task["attempt_count"]), (2, 2))
            self.assertEqual(task["last_outcome_status"], "failed")

    def test_open_traycer_bridge_run_joins_without_a_synthetic_finish(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            _write_events(
                spool,
                1,
                [
                    _started(event_number=1, run_number=1),
                    _bound(
                        event_number=2,
                        run_number=1,
                        attempt_number=1,
                        native_id=CLAUDE_SESSION,
                    ),
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                _insert_session(
                    conn,
                    session_id=CLAUDE_SESSION,
                    source="claudecode",
                )
                result = ingest_subfleet_events(conn, spool)
                timeline = get_task_timeline(conn, _ref("task", 1))
                task = list_tasks(conn)[0]

            self.assertEqual((result.inserted, result.reconciled), (2, 1))
            self.assertEqual(
                [event["event_type"] for event in timeline],
                ["run.started", "run.bound"],
            )
            self.assertEqual(timeline[-1]["session_id"], CLAUDE_SESSION)
            self.assertEqual((task["run_count"], task["attempt_count"]), (1, 1))
            self.assertIsNone(task["last_outcome_status"])

    def test_out_of_order_events_reconcile_after_session_and_spool_are_gone(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            event_path = _write_events(
                spool,
                1,
                [
                    _finished(event_number=3, run_number=1, attempt_number=1),
                    _bound(
                        event_number=2,
                        run_number=1,
                        attempt_number=1,
                        native_id=CLAUDE_SESSION,
                    ),
                    _started(event_number=1, run_number=1),
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                first = ingest_subfleet_events(conn, spool)
                attempt = conn.execute(
                    "SELECT session_id FROM subfleet_attempts WHERE attempt_id = ?",
                    (_ref("attempt", 1),),
                ).fetchone()
                self.assertIsNone(attempt["session_id"])

            event_path.unlink()
            shutil.rmtree(spool)
            with get_db(db_path) as conn:
                _insert_session(
                    conn,
                    session_id=CLAUDE_SESSION,
                    source="claudecode",
                )
                second = ingest_subfleet_events(conn, spool)
                timeline = get_task_timeline(conn, _ref("task", 1))

            self.assertEqual((first.inserted, first.reconciled), (3, 0))
            self.assertEqual(
                (second.inserted, second.duplicates, second.reconciled),
                (0, 0, 1),
            )
            self.assertEqual(len(timeline), 3)
            self.assertEqual(timeline[-1]["session_id"], CLAUDE_SESSION)

    def test_conflicting_replay_and_attempt_identity_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            path = _write_events(
                spool,
                1,
                [
                    _started(event_number=1, run_number=1),
                    _bound(
                        event_number=2,
                        run_number=1,
                        attempt_number=1,
                        native_id=CLAUDE_SESSION,
                    ),
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                first = ingest_subfleet_events(conn, spool)

            conflicting_start = _started(event_number=1, run_number=1)
            conflicting_start["lane_ref"] = _ref("lane", 99)
            duplicate_start = _started(event_number=5, run_number=1)
            duplicate_binding = _bound(
                event_number=6,
                run_number=1,
                attempt_number=1,
                native_id=CLAUDE_SESSION,
            )
            path.write_text(
                "".join(
                    f"{json.dumps(event)}\n"
                    for event in (
                        conflicting_start,
                        duplicate_start,
                        duplicate_binding,
                    )
                ),
                encoding="utf-8",
            )
            path.chmod(0o600)
            _write_events(
                spool,
                2,
                [
                    _bound(
                        event_number=3,
                        run_number=2,
                        attempt_number=2,
                        native_id=CLAUDE_SESSION,
                    )
                ],
            )
            _write_events(
                spool,
                3,
                [
                    _bound(
                        event_number=4,
                        run_number=3,
                        attempt_number=1,
                        native_id=CLAUDE_SESSION_2,
                    )
                ],
            )
            with get_db(db_path) as conn:
                second = ingest_subfleet_events(conn, spool)
                event = conn.execute(
                    "SELECT lane_ref FROM subfleet_events WHERE event_id = ?",
                    (_ref("event", 1),),
                ).fetchone()
                attempt_count = conn.execute(
                    "SELECT COUNT(*) FROM subfleet_attempts"
                ).fetchone()[0]

            self.assertEqual(first.inserted, 2)
            self.assertEqual((second.inserted, second.rejected), (0, 5))
            self.assertEqual(event["lane_ref"], _ref("lane", 1))
            self.assertEqual(attempt_count, 1)

    def test_only_direct_allowlisted_spool_records_are_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            secret = "sk-private-never-store"
            unexpected = _started(event_number=1, run_number=1)
            unexpected["prompt"] = secret
            oversized = _finished(
                event_number=4,
                run_number=1,
                attempt_number=None,
            )
            oversized["outcome"]["duration_s"] = 10**1000
            overflow_timestamp = _started(
                event_number=5,
                run_number=1,
                occurred_at="9999-12-31T23:59:59-23:59",
            )
            unsafe_handoff = _handoff(
                event_number=6,
                run_number=1,
                provider="codex",
                source_attempt_number=1,
                source_native_id=CLAUDE_SESSION,
            )
            unsafe_handoff["prompt"] = secret
            _write_events(
                spool,
                1,
                [unexpected, oversized, overflow_timestamp, unsafe_handoff],
            )

            outside = root / "private-run.jsonl"
            outside.write_text(
                f"{json.dumps(_started(event_number=2, run_number=2))}\n{secret}\n",
                encoding="utf-8",
            )
            (spool / f"{_ref('run', 2)}.jsonl").symlink_to(outside)
            nested = spool / "private-runs"
            nested.mkdir()
            _write_events(nested, 3, [_started(event_number=3, run_number=3)])

            init_db(db_path)
            with get_db(db_path) as conn:
                result = ingest_subfleet_events(conn, spool)
                count = conn.execute("SELECT COUNT(*) FROM subfleet_events").fetchone()[
                    0
                ]
                columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(subfleet_events)"
                    ).fetchall()
                }
                handoff_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(subfleet_handoffs)"
                    ).fetchall()
                }

            self.assertEqual((result.inserted, result.rejected), (0, 5))
            self.assertEqual(count, 0)
            self.assertNotIn("raw_json", columns)
            self.assertNotIn("source_path", columns)
            self.assertNotIn("raw_json", handoff_columns)
            self.assertNotIn("source_path", handoff_columns)
            self.assertNotIn("prompt", handoff_columns)
            with self.assertRaises(SubfleetSpoolError), get_db(db_path) as conn:
                ingest_subfleet_events(conn, spool.parent)

    def test_rejects_symlinked_parent_hardlinks_and_non_private_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            init_db(db_path)

            real_events = root / "real-events"
            real_spool = real_events / "v1"
            real_spool.mkdir(parents=True)
            _write_events(real_spool, 1, [_started(event_number=1, run_number=1)])
            linked_state = root / "linked-state"
            linked_state.mkdir()
            (linked_state / "integration-events").symlink_to(real_events)
            with self.assertRaises(SubfleetSpoolError), get_db(db_path) as conn:
                ingest_subfleet_events(conn, linked_state / "integration-events" / "v1")

            spool = _spool(root / "safe")
            hardlink_source = root / "private-run.jsonl"
            hardlink_source.write_text(
                f"{json.dumps(_started(event_number=2, run_number=2))}\n",
                encoding="utf-8",
            )
            hardlink_source.chmod(0o600)
            os.link(hardlink_source, spool / f"{_ref('run', 2)}.jsonl")
            public_file = _write_events(
                spool, 3, [_started(event_number=3, run_number=3)]
            )
            public_file.chmod(0o644)
            fifo_name = f"{_ref('run', 4)}.jsonl"
            os.mkfifo(spool / fifo_name, 0o600)
            original_open = os.open

            def require_nonblocking_fifo(path, flags, *args, **kwargs):
                if path == fifo_name:
                    self.assertTrue(flags & os.O_NONBLOCK)
                return original_open(path, flags, *args, **kwargs)

            with (
                mock.patch(
                    "logpile.subfleet.os.open",
                    side_effect=require_nonblocking_fifo,
                ),
                get_db(db_path) as conn,
            ):
                result = ingest_subfleet_events(conn, spool)
                count = conn.execute("SELECT COUNT(*) FROM subfleet_events").fetchone()[
                    0
                ]

            self.assertEqual((result.inserted, result.rejected), (0, 3))
            self.assertEqual(count, 0)

    def test_directory_swap_after_open_cannot_redirect_ingestion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            _write_events(spool, 1, [_started(event_number=1, run_number=1)])
            replacement = root / "private-replacement"
            replacement.mkdir()
            _write_events(replacement, 2, [_started(event_number=2, run_number=2)])
            moved_spool = root / "original-v1"
            original_listdir = os.listdir

            def swap_before_enumeration(directory_fd):
                spool.rename(moved_spool)
                spool.symlink_to(replacement, target_is_directory=True)
                return original_listdir(directory_fd)

            init_db(db_path)
            with (
                mock.patch(
                    "logpile.subfleet.os.listdir",
                    side_effect=swap_before_enumeration,
                ),
                get_db(db_path) as conn,
            ):
                result = ingest_subfleet_events(conn, spool)
                event_ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT event_id FROM subfleet_events ORDER BY event_id"
                    ).fetchall()
                ]

            self.assertEqual((result.inserted, result.rejected), (1, 0))
            self.assertEqual(event_ids, [_ref("event", 1)])

    def test_retention_prune_between_list_and_open_is_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            event_path = _write_events(
                spool,
                1,
                [_started(event_number=1, run_number=1)],
            )
            original_listdir = os.listdir

            def prune_after_list(directory_fd):
                entries = original_listdir(directory_fd)
                os.unlink(event_path.name, dir_fd=directory_fd)
                return entries

            init_db(db_path)
            with (
                mock.patch(
                    "logpile.subfleet.os.listdir",
                    side_effect=prune_after_list,
                ),
                get_db(db_path) as conn,
            ):
                result = ingest_subfleet_events(conn, spool)
                count = conn.execute("SELECT COUNT(*) FROM subfleet_events").fetchone()[
                    0
                ]

            self.assertEqual((result.inserted, result.rejected), (0, 0))
            self.assertEqual(count, 0)

    def test_non_missing_spool_open_error_remains_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            event_path = _write_events(
                spool,
                1,
                [_started(event_number=1, run_number=1)],
            )
            original_open = os.open

            def deny_event_open(path, flags, *args, **kwargs):
                if path == event_path.name:
                    raise PermissionError("retention did not remove this file")
                return original_open(path, flags, *args, **kwargs)

            init_db(db_path)
            with (
                mock.patch(
                    "logpile.subfleet.os.open",
                    side_effect=deny_event_open,
                ),
                get_db(db_path) as conn,
            ):
                result = ingest_subfleet_events(conn, spool)

            self.assertEqual((result.inserted, result.rejected), (0, 1))

    def test_sync_ingests_and_reconciles_after_native_transcript_parse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            transcript = (
                home / ".claude" / "projects" / "-tmp-demo" / f"{CLAUDE_SESSION}.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (
                        {
                            "timestamp": "2026-08-28T10:00:00Z",
                            "type": "user",
                            "cwd": "/tmp/demo",
                            "message": {"content": "build it"},
                        },
                        {
                            "timestamp": "2026-08-28T10:00:01Z",
                            "type": "assistant",
                            "message": {
                                "id": "msg-1",
                                "model": "claude-test",
                                "usage": {"input_tokens": 1, "output_tokens": 1},
                                "content": [{"type": "text", "text": "done"}],
                            },
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            spool = _spool(root)
            _write_events(
                spool,
                1,
                [
                    _started(event_number=1, run_number=1),
                    _bound(
                        event_number=2,
                        run_number=1,
                        attempt_number=1,
                        native_id=CLAUDE_SESSION,
                    ),
                ],
            )
            rejected = _started(event_number=3, run_number=2)
            rejected["prompt"] = "private prompt"
            _write_events(spool, 2, [rejected])

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = sync_sessions(
                    shared_dir=shared,
                    db_path=db_path,
                    username="alice",
                    machine="test-machine",
                    home=home,
                    subfleet_events_dir=spool,
                )
            with get_db(db_path) as conn:
                attempt = conn.execute(
                    "SELECT session_id FROM subfleet_attempts WHERE attempt_id = ?",
                    (_ref("attempt", 1),),
                ).fetchone()

            self.assertEqual(result.new, 1)
            self.assertEqual(attempt["session_id"], CLAUDE_SESSION)
            self.assertIn("rejected 1 unsafe or malformed", stderr.getvalue())
            self.assertNotIn("private prompt", stderr.getvalue())

    def test_local_cli_emits_joined_timeline_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            _write_events(spool, 1, [_started(event_number=1, run_number=1)])
            init_db(db_path)
            with get_db(db_path) as conn:
                ingest_subfleet_events(conn, spool)

            result = CliRunner().invoke(
                cli,
                [
                    "task-timeline",
                    _ref("task", 1),
                    "--db",
                    str(db_path),
                    "--json",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["backend"], "local")
            self.assertEqual(payload["events"][0]["event_type"], "run.started")

            task_list = CliRunner().invoke(
                cli,
                ["task-list", "--db", str(db_path), "--json"],
            )
            self.assertEqual(task_list.exit_code, 0, task_list.output)
            task_payload = json.loads(task_list.output)
            self.assertEqual(task_payload["tasks"][0]["task_id"], _ref("task", 1))
            self.assertEqual(task_payload["tasks"][0]["providers"], ["claude"])

    def test_task_queries_normalize_pre_handoff_views(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            with get_db(db_path) as conn:
                conn.execute("DROP VIEW subfleet_task_catalog")
                conn.execute("DROP VIEW subfleet_task_timeline")
                conn.execute(
                    f"""
                    CREATE VIEW subfleet_task_catalog AS
                    SELECT
                        '{_ref("task", 1)}' AS task_id,
                        '2026-08-28T10:00:00+00:00' AS first_occurred_at,
                        '2026-08-28T10:00:00+00:00' AS last_occurred_at,
                        1 AS has_claude,
                        0 AS has_codex,
                        0 AS has_unknown,
                        1 AS run_count,
                        0 AS attempt_count,
                        NULL AS last_outcome_status,
                        NULL AS last_outcome_exit_code,
                        NULL AS last_outcome_finished_at
                    """
                )
                conn.execute(
                    f"""
                    CREATE VIEW subfleet_task_timeline AS
                    SELECT
                        '{_ref("event", 1)}' AS event_id,
                        1 AS schema_version,
                        'run.started' AS event_type,
                        '{_ref("task", 1)}' AS task_id,
                        '{_ref("run", 1)}' AS run_id,
                        NULL AS attempt_id,
                        'claude' AS provider,
                        NULL AS lane_ref,
                        NULL AS workspace_ref,
                        '2026-08-28T10:00:00+00:00' AS occurred_at,
                        '2026-08-28T10:00:00+00:00' AS started_at,
                        NULL AS finished_at,
                        NULL AS binding_kind,
                        NULL AS native_id,
                        NULL AS outcome_status,
                        NULL AS outcome_exit_code,
                        NULL AS outcome_duration_s,
                        NULL AS session_id,
                        NULL AS session_source,
                        NULL AS session_project,
                        NULL AS session_first_timestamp,
                        NULL AS session_last_timestamp,
                        NULL AS session_status
                    """
                )

            json_result = CliRunner().invoke(
                cli,
                ["task-list", "--db", str(db_path), "--json"],
            )
            human_result = CliRunner().invoke(
                cli,
                ["task-list", "--db", str(db_path)],
            )
            timeline_result = CliRunner().invoke(
                cli,
                [
                    "task-timeline",
                    _ref("task", 1),
                    "--db",
                    str(db_path),
                    "--json",
                ],
            )

            self.assertEqual(json_result.exit_code, 0, json_result.output)
            self.assertEqual(
                json.loads(json_result.output)["tasks"][0]["handoff_count"],
                0,
            )
            self.assertEqual(human_result.exit_code, 0, human_result.output)
            self.assertIn("handoffs=0", human_result.output)
            self.assertEqual(timeline_result.exit_code, 0, timeline_result.output)
            timeline_event = json.loads(timeline_result.output)["events"][0]
            for column in (
                "source_attempt_id",
                "source_provider",
                "source_binding_kind",
                "source_native_id",
                "source_session_id",
                "source_session_source",
                "source_session_project",
            ):
                self.assertIn(column, timeline_event)
                self.assertIsNone(timeline_event[column])

    def test_task_cli_reads_while_writer_transaction_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            spool = _spool(root)
            _write_events(spool, 1, [_started(event_number=1, run_number=1)])
            init_db(db_path)
            with get_db(db_path) as conn:
                ingest_subfleet_events(conn, spool)

            writer = sqlite3.connect(db_path)
            try:
                writer.execute("BEGIN IMMEDIATE")
                task_list = CliRunner().invoke(
                    cli,
                    ["task-list", "--db", str(db_path), "--json"],
                )
                timeline = CliRunner().invoke(
                    cli,
                    [
                        "task-timeline",
                        _ref("task", 1),
                        "--db",
                        str(db_path),
                        "--json",
                    ],
                )
            finally:
                writer.rollback()
                writer.close()

            self.assertEqual(task_list.exit_code, 0, task_list.output)
            self.assertEqual(timeline.exit_code, 0, timeline.output)
            self.assertEqual(
                json.loads(task_list.output)["tasks"][0]["task_id"],
                _ref("task", 1),
            )
            self.assertEqual(
                json.loads(timeline.output)["events"][0]["event_type"],
                "run.started",
            )

    def test_readonly_query_sees_wal_commit_created_at_connect(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            real_connect = sqlite3.connect
            writer_connections = []

            def commit_then_connect(*args, **kwargs):
                writer = real_connect(db_path)
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute(
                    """
                    INSERT INTO subfleet_events (
                        event_id, schema_version, event_type, task_id, run_id,
                        attempt_id, provider, lane_ref, workspace_ref,
                        occurred_at, started_at, finished_at, binding_kind,
                        binding_native_id, outcome_status, outcome_exit_code,
                        outcome_duration_s, ingested_at
                    ) VALUES (?, 1, 'run.started', ?, ?, NULL, 'claude',
                              NULL, NULL, ?, ?, NULL, NULL, NULL, NULL, NULL,
                              NULL, ?)
                    """,
                    (
                        _ref("event", 1),
                        _ref("task", 1),
                        _ref("run", 1),
                        "2026-08-28T10:00:00+00:00",
                        "2026-08-28T10:00:00+00:00",
                        "2026-08-28T10:00:00+00:00",
                    ),
                )
                writer.commit()
                writer_connections.append(writer)
                return real_connect(*args, **kwargs)

            try:
                with mock.patch(
                    "logpile.db.sqlite3.connect",
                    side_effect=commit_then_connect,
                ):
                    result = CliRunner().invoke(
                        cli,
                        ["task-list", "--db", str(db_path), "--json"],
                    )
            finally:
                for writer in writer_connections:
                    writer.close()

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(
                json.loads(result.output)["tasks"][0]["task_id"],
                _ref("task", 1),
            )

    def test_task_cli_does_not_migrate_uninitialized_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE sentinel (value TEXT)")
            before_mtime = db_path.stat().st_mtime_ns
            with sqlite3.connect(db_path) as conn:
                before_schema = conn.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                ).fetchall()
            before_entries = sorted(path.name for path in db_path.parent.iterdir())

            results = [
                CliRunner().invoke(
                    cli,
                    ["task-list", "--db", str(db_path), "--json"],
                ),
                CliRunner().invoke(
                    cli,
                    [
                        "task-timeline",
                        _ref("task", 1),
                        "--db",
                        str(db_path),
                        "--json",
                    ],
                ),
            ]

            with sqlite3.connect(db_path) as conn:
                after_schema = conn.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                ).fetchall()
            self.assertTrue(
                all(result.exit_code != 0 for result in results),
                [result.output for result in results],
            )
            for result in results:
                self.assertIn("Task schema is not initialized", result.output)
            self.assertEqual(after_schema, before_schema)
            self.assertEqual(db_path.stat().st_mtime_ns, before_mtime)
            self.assertEqual(
                sorted(path.name for path in db_path.parent.iterdir()),
                before_entries,
            )

    def test_task_cli_maps_corrupt_database_errors_to_click_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            db_path.write_bytes(b"this is not a SQLite database")
            commands = (
                ["task-list", "--db", str(db_path), "--json"],
                [
                    "task-timeline",
                    _ref("task", 1),
                    "--db",
                    str(db_path),
                    "--json",
                ],
            )

            for command in commands:
                with self.subTest(command=command[0]):
                    result = CliRunner().invoke(cli, command)
                    self.assertEqual(result.exit_code, 1, result.output)
                    self.assertIn(
                        "Could not read local task database",
                        result.output,
                    )
                    self.assertNotIn("Traceback", result.output)

    def test_invalid_spool_override_fails_before_local_or_cloud_sync(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            invalid_spool = root / "private-runs"
            with (
                mock.patch("logpile.sync.sync_sessions") as local_sync,
                mock.patch("logpile.backup.push_backup") as cloud_sync,
            ):
                result = CliRunner().invoke(
                    cli,
                    [
                        "sync",
                        "--backend",
                        "both",
                        "--db",
                        str(root / "logpile.db"),
                        "--shared",
                        str(root / "shared"),
                        "--db-url",
                        "postgresql://example.invalid/logpile",
                        "--subfleet-events-dir",
                        str(invalid_spool),
                    ],
                )

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("dedicated integration-events/v1 spool", result.output)
            self.assertNotIn("Syncing local sessions", result.output)
            local_sync.assert_not_called()
            cloud_sync.assert_not_called()

    def test_direct_sync_preflights_spool_before_creating_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            shared = root / "shared"

            with self.assertRaises(SubfleetSpoolError):
                sync_sessions(
                    shared_dir=shared,
                    db_path=db_path,
                    username="alice",
                    machine="test-machine",
                    home=root / "home",
                    subfleet_events_dir=root / "private-runs",
                )

            self.assertFalse(db_path.exists())
            self.assertFalse(shared.exists())
            self.assertFalse(Path(f"{db_path}.sync.lock").exists())

    def test_task_cli_reads_clean_wal_database_in_readonly_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            init_db(db_path)
            self.assertFalse(Path(f"{db_path}-wal").exists())
            self.assertFalse(Path(f"{db_path}-shm").exists())
            before_entries = sorted(path.name for path in root.iterdir())
            db_path.chmod(0o400)
            root.chmod(0o500)
            try:
                result = CliRunner().invoke(
                    cli,
                    ["task-list", "--db", str(db_path), "--json"],
                )
                after_entries = sorted(path.name for path in root.iterdir())
            finally:
                root.chmod(0o700)
                db_path.chmod(0o600)

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(json.loads(result.output)["tasks"], [])
            self.assertEqual(after_entries, before_entries)


if __name__ == "__main__":
    unittest.main()
