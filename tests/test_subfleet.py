import io
import json
import os
import shutil
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
            _write_events(spool, 1, [unexpected, oversized, overflow_timestamp])

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

            self.assertEqual((result.inserted, result.rejected), (0, 4))
            self.assertEqual(count, 0)
            self.assertNotIn("raw_json", columns)
            self.assertNotIn("source_path", columns)
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


if __name__ == "__main__":
    unittest.main()
