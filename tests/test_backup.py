import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from logpile.backup import (
    R2Config,
    SUPABASE_SCHEMA_SQL,
    build_candidate,
    create_sqlite_snapshot,
    discover_raw_paths,
    infer_jsonl_source,
    iter_text_chunks,
    plan_backup,
    push_backup,
    snapshot_candidate,
)
from logpile.db import init_db


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record))
            fh.write("\n")


class BackupTests(unittest.TestCase):
    def test_plan_discovers_all_roots_rotated_shared_rows_and_deduplicates_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            shared = home / "logpile" / "shared"
            db_path = home / "logpile" / "logpile.db"
            primary_records = [
                {
                    "type": "session_meta",
                    "payload": {"id": "codex-1", "timestamp": "2026-05-16T10:00:00Z"},
                }
            ]
            write_jsonl(
                home / ".codex" / "sessions" / "2026" / "05" / "16" / "rollout.jsonl",
                primary_records,
            )
            write_jsonl(
                home / ".codex" / "archived_sessions" / "old.jsonl",
                [{"type": "session_meta", "payload": {"id": "archive-1"}}],
            )
            codex_2 = home / ".codex-2" / "sessions" / "codex-2.jsonl"
            codex_3 = home / ".codex-3" / "sessions" / "codex-3.jsonl"
            openclaw = (
                home
                / ".openclaw"
                / "agents"
                / "bot"
                / "agent"
                / "codex-home"
                / "sessions"
                / "openclaw.jsonl"
            )
            write_jsonl(codex_2, [{"type": "session_meta", "payload": {"id": "codex-2"}}])
            write_jsonl(codex_3, [{"type": "session_meta", "payload": {"id": "codex-3"}}])
            write_jsonl(openclaw, [{"type": "session_meta", "payload": {"id": "openclaw"}}])
            # A second native path with byte-identical content must not produce
            # a second backup candidate/object manifest.
            duplicate = home / ".codex-2" / "sessions" / "duplicate.jsonl"
            write_jsonl(duplicate, primary_records)
            write_jsonl(
                home / ".claude" / "projects" / "-tmp-project" / "claude.jsonl",
                [{"type": "user", "sessionId": "claude-1", "message": {"content": "hi"}}],
            )
            rotated_shared = shared / "alice" / "claudecode" / "demo" / "rotated.jsonl"
            write_jsonl(
                rotated_shared,
                [
                    {
                        "type": "user",
                        "sessionId": "rotated-only",
                        "message": {"content": "sole surviving shared artifact"},
                    }
                ],
            )
            init_db(db_path)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, source, username, source_path, shared_path
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "rotated-only",
                        "claudecode",
                        "alice",
                        str(home / ".claude" / "projects" / "gone" / "rotated.jsonl"),
                        str(rotated_shared),
                    ),
                )
            codex_db = home / ".codex" / "logs_2.sqlite"
            codex_db.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(codex_db) as conn:
                conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")

            discovered = set(
                discover_raw_paths(
                    home,
                    db_path=db_path,
                    shared_dir=shared,
                )
            )
            plan = plan_backup(
                home=home,
                db_path=db_path,
                shared_dir=shared,
            )

            self.assertTrue({codex_2, codex_3, openclaw, duplicate, rotated_shared} <= discovered)
            self.assertEqual(len(discovered), 9)
            self.assertEqual(len(plan.candidates), 8)
            self.assertEqual(len({candidate.sha256 for candidate in plan.candidates}), 8)
            self.assertEqual(plan.source_counts["codex"], 4)
            self.assertEqual(plan.source_counts["codex_archive"], 1)
            self.assertEqual(plan.source_counts["claudecode"], 2)
            self.assertEqual(plan.source_counts["codex_db"], 1)
            self.assertIn(rotated_shared, {candidate.path for candidate in plan.candidates})
            self.assertTrue(all(len(candidate.sha256) == 64 for candidate in plan.candidates))
            self.assertTrue(all(candidate.object_key.startswith("raw/sha256/") for candidate in plan.candidates))

    def test_db_managed_private_reviewed_and_reused_artifacts_are_included(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            shared = home / "logpile" / "shared"
            private = shared.parent / f".{shared.name}-private"
            db_path = home / "logpile" / "logpile.db"

            reused_source = home / ".codex" / "sessions" / "reused.jsonl"
            reused_archive = shared / "alice" / "codex" / "demo" / "reused.jsonl"
            private_archive = private / "alice" / "claudecode" / "demo" / "private.jsonl"
            reviewed_artifact = (
                shared / ".published" / "reviewed" / ("a" * 64 + ".jsonl")
            )
            write_jsonl(reused_source, [{"type": "session_meta", "payload": {"id": "new"}}])
            write_jsonl(reused_archive, [{"type": "session_meta", "payload": {"id": "old"}}])
            write_jsonl(private_archive, [{"type": "user", "message": {"content": "private"}}])
            write_jsonl(reviewed_artifact, [{"type": "user", "message": {"content": "reviewed"}}])

            init_db(db_path)
            with sqlite3.connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO sessions (
                        session_id, source, username, source_path, shared_path,
                        reviewed_artifact_path
                    ) VALUES (?, ?, 'alice', ?, ?, ?)
                    """,
                    (
                        (
                            "reused",
                            "codex",
                            str(reused_source),
                            str(reused_archive),
                            None,
                        ),
                        (
                            "private",
                            "claudecode",
                            str(home / "gone-private.jsonl"),
                            str(private_archive),
                            None,
                        ),
                        (
                            "reviewed",
                            "claudecode",
                            str(home / "gone-reviewed.jsonl"),
                            "",
                            str(reviewed_artifact),
                        ),
                    ),
                )

            discovered = set(
                discover_raw_paths(
                    home,
                    db_path=db_path,
                    shared_dir=shared,
                    include_codex_db=False,
                )
            )
            self.assertEqual(
                discovered,
                {reused_source, reused_archive, private_archive, reviewed_artifact},
            )
            plan = plan_backup(
                home=home,
                db_path=db_path,
                shared_dir=shared,
                include_codex_db=False,
            )
            self.assertEqual(len(plan.candidates), 4)
            self.assertEqual(len({candidate.sha256 for candidate in plan.candidates}), 4)

    def test_configured_corrupt_logpile_db_fails_backup_discovery_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            shared = home / "logpile" / "shared"
            artifact = shared / "alice" / "codex" / "only-copy.jsonl"
            write_jsonl(
                artifact,
                [{"type": "session_meta", "payload": {"id": "only-copy"}}],
            )
            db_path = home / "logpile" / "logpile.db"
            db_path.write_bytes(b"this is not a SQLite database")

            with self.assertRaisesRegex(
                RuntimeError,
                r"Could not read configured Logpile database.*file is not a database",
            ):
                plan_backup(
                    home=home,
                    db_path=db_path,
                    shared_dir=shared,
                    include_codex_db=False,
                )

    def test_sessions_query_error_fails_backup_discovery_closed(self) -> None:
        class FailingSessionsQueryConnection:
            row_factory = None
            closed = False

            def execute(self, sql: str):
                if sql.lstrip().startswith("PRAGMA table_info"):
                    return [
                        (0, "source"),
                        (1, "source_path"),
                        (2, "shared_path"),
                        (3, "reviewed_artifact_path"),
                    ]
                raise sqlite3.OperationalError("injected sessions query failure")

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            shared = home / "logpile" / "shared"
            shared.mkdir(parents=True)
            db_path = home / "logpile" / "logpile.db"
            with sqlite3.connect(db_path):
                pass
            failing_connection = FailingSessionsQueryConnection()

            with patch(
                "logpile.discovery.sqlite3.connect",
                return_value=failing_connection,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"Could not read configured Logpile database.*injected sessions query failure",
                ):
                    list(
                        discover_raw_paths(
                            home,
                            db_path=db_path,
                            shared_dir=shared,
                            include_codex_db=False,
                        )
                    )
            self.assertTrue(failing_connection.closed)

    def test_valid_non_logpile_or_missing_db_remains_optional(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            shared = home / "logpile" / "shared"
            db_path = home / "logpile" / "other.sqlite"
            db_path.parent.mkdir(parents=True)
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE unrelated (value TEXT)")

            self.assertEqual(
                list(
                    discover_raw_paths(
                        home,
                        db_path=db_path,
                        shared_dir=shared,
                        include_codex_db=False,
                    )
                ),
                [],
            )
            self.assertEqual(
                list(
                    discover_raw_paths(
                        home,
                        db_path=home / "logpile" / "missing.sqlite",
                        shared_dir=shared,
                        include_codex_db=False,
                    )
                ),
                [],
            )

    def test_sqlite_snapshot_includes_committed_wal_and_excludes_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            source = home / ".codex" / "logs_2.sqlite"
            source.parent.mkdir(parents=True)
            writer = sqlite3.connect(source)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE events (value TEXT)")
                writer.commit()
                writer.execute("INSERT INTO events VALUES ('committed in wal')")
                writer.commit()
                self.assertTrue(Path(f"{source}-wal").exists())

                paths = list(discover_raw_paths(home, include_codex_db=True))
                self.assertEqual(paths, [source])

                with snapshot_candidate(source, home=home) as candidate:
                    with sqlite3.connect(candidate.payload_path) as snapshot:
                        values = snapshot.execute("SELECT value FROM events").fetchall()
                        check = snapshot.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                writer.close()

        self.assertEqual(values, [("committed in wal",)])
        self.assertEqual(check, "ok")

    def test_create_sqlite_snapshot_atomically_replaces_verified_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.db"
            destination = root / "backup.db"
            destination.write_bytes(b"old incomplete backup")
            with sqlite3.connect(source) as conn:
                conn.execute("CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO settings VALUES ('visibility', 'private')")

            previous_umask = os.umask(0o022)
            try:
                result = create_sqlite_snapshot(source, destination)
            finally:
                os.umask(previous_umask)

            with sqlite3.connect(destination) as conn:
                row = conn.execute("SELECT value FROM settings WHERE name = 'visibility'").fetchone()
                check = conn.execute("PRAGMA quick_check").fetchone()[0]
            destination_mode = destination.stat().st_mode & 0o777

        self.assertEqual(result, destination)
        self.assertEqual(row, ("private",))
        self.assertEqual(check, "ok")
        self.assertEqual(destination_mode, 0o600)

    def test_failed_sqlite_snapshot_leaves_existing_output_intact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "corrupt.db"
            destination = root / "last-good.db"
            source.write_bytes(b"not a sqlite database")
            destination.write_bytes(b"last known good backup")

            with self.assertRaisesRegex(RuntimeError, "Could not snapshot SQLite"):
                create_sqlite_snapshot(source, destination)

            self.assertEqual(destination.read_bytes(), b"last known good backup")
            self.assertEqual(list(root.glob(".last-good.db.*.tmp")), [])

    def test_snapshot_candidate_reads_from_stable_payload_when_source_grows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            path = home / ".codex" / "sessions" / "2026" / "05" / "16" / "rollout.jsonl"
            write_jsonl(
                path,
                [
                    {"type": "session_meta", "payload": {"id": "snapshot-1"}},
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "stable payload"}],
                        },
                    },
                ],
            )
            original = path.read_bytes()

            with snapshot_candidate(path, home=home) as candidate:
                with path.open("ab") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "later append"}],
                                },
                            }
                        ).encode("utf-8")
                    )
                    fh.write(b"\n")

                self.assertEqual(candidate.size_bytes, len(original))
                self.assertEqual(candidate.sha256, hashlib.sha256(original).hexdigest())
                self.assertEqual(candidate.payload_path.read_bytes(), original)
                text = "\n".join(chunk.content for chunk in iter_text_chunks(candidate))

            self.assertIn("stable payload", text)
            self.assertNotIn("later append", text)
            self.assertFalse(candidate.payload_path.exists())

    def test_iter_text_chunks_preserves_exact_codex_tool_output(self) -> None:
        long_output = "prefix\n" + ("A" * 6000) + "\nUNIQUE_TAIL_123"
        records = [
            {
                "timestamp": "2026-05-16T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "codex-search-1"},
            },
            {
                "timestamp": "2026-05-16T10:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "find the session where I asked about sb db raw logs",
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-05-16T10:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": long_output,
                },
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            path = home / ".codex" / "sessions" / "2026" / "05" / "16" / "rollout.jsonl"
            write_jsonl(path, records)

            candidate = build_candidate(path, home=home)
            chunks = list(iter_text_chunks(candidate, max_chars=2000, overlap_chars=0))

        user_text = "\n".join(chunk.content for chunk in chunks if chunk.role == "user")
        self.assertIn("sb db raw logs", user_text)

        tool_output = "".join(
            chunk.content
            for chunk in chunks
            if chunk.event_index == 3 and chunk.role == "tool_result"
        )
        self.assertEqual(tool_output, long_output)
        self.assertIn("UNIQUE_TAIL_123", tool_output)
        self.assertEqual({chunk.session_id for chunk in chunks}, {"codex-search-1"})

    def test_iter_text_chunks_infers_agent_source_for_shared_rows(self) -> None:
        records = [
            {
                "timestamp": "2026-05-16T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "shared-codex-1"},
            },
            {
                "timestamp": "2026-05-16T10:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "shared codex source"}],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            path = home / "logpile" / "shared" / "max" / "codex" / "demo" / "rollout.jsonl"
            write_jsonl(path, records)

            candidate = replace(
                build_candidate(path, home=home),
                source="logpile_shared",
                relative_path="logpile/shared/max/codex/demo/rollout.jsonl",
            )
            chunks = list(iter_text_chunks(candidate))

        self.assertEqual({chunk.session_id for chunk in chunks}, {"shared-codex-1"})
        self.assertEqual({chunk.source for chunk in chunks}, {"codex"})

    def test_iter_text_chunks_extracts_claude_tool_inputs(self) -> None:
        records = [
            {
                "type": "user",
                "timestamp": "2026-05-16T10:00:00Z",
                "sessionId": "claude-search-1",
                "message": {"role": "user", "content": "find exact thing x"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-16T10:00:01Z",
                "sessionId": "claude-search-1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I'll search."},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "rg 'exact thing x'"},
                        },
                    ],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            path = home / ".claude" / "projects" / "-tmp-project" / "session.jsonl"
            write_jsonl(path, records)

            candidate = build_candidate(path, home=home)
            chunks = list(iter_text_chunks(candidate))

        self.assertIn("find exact thing x", "\n".join(chunk.content for chunk in chunks))
        tool_chunks = [chunk for chunk in chunks if chunk.role == "tool_use"]
        self.assertEqual(len(tool_chunks), 1)
        self.assertIn("rg 'exact thing x'", tool_chunks[0].content)
        self.assertEqual(tool_chunks[0].tool_name, "Bash")

    def test_schema_supports_exact_keyword_search(self) -> None:
        self.assertIn("logpile_raw_chunks", SUPABASE_SCHEMA_SQL)
        self.assertIn("to_tsvector('english', content)", SUPABASE_SCHEMA_SQL)
        self.assertNotIn("gin_trgm_ops", SUPABASE_SCHEMA_SQL)

    def test_infer_jsonl_source_distinguishes_agent_formats(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            codex_path = root / "codex.jsonl"
            claude_path = root / "claude.jsonl"
            write_jsonl(
                codex_path,
                [
                    {"record_type": "state"},
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    },
                ],
            )
            write_jsonl(
                claude_path,
                [
                    {
                        "sessionId": "claude-1",
                        "uuid": "event-1",
                        "type": "user",
                        "message": {"role": "user", "content": "hello"},
                    }
                ],
            )

            self.assertEqual(infer_jsonl_source(codex_path), "codex")
            self.assertEqual(infer_jsonl_source(claude_path), "claudecode")

    def test_push_backup_uploads_and_indexes_with_injected_clients(self) -> None:
        import logpile.backup as backup

        uploaded_keys: list[str] = []
        upserted_files: list[str] = []
        indexed_files: list[str] = []

        class FakeStore:
            def __init__(self, config):
                self.config = config

            def upload(self, candidate):
                uploaded_keys.append(candidate.object_key)
                return True

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class FakeArchive:
            def __init__(self, db_url):
                self.db_url = db_url

            def ensure_schema(self, *, create_search_index=True):
                pass

            def connect(self):
                return FakeConnection()

            def upsert_file(self, conn, candidate, *, provider, bucket):
                upserted_files.append(candidate.relative_path)

            def replace_chunks(self, conn, candidate, chunks, *, batch_size=1000):
                indexed_files.append(candidate.relative_path)
                return sum(1 for _ in chunks)

        original_store = backup.S3ObjectStore
        original_archive = backup.SupabaseArchive
        backup.S3ObjectStore = FakeStore
        backup.SupabaseArchive = FakeArchive
        try:
            with tempfile.TemporaryDirectory() as td:
                home = Path(td)
                records = [
                    {"type": "session_meta", "payload": {"id": "push-1"}},
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "find raw log cloud backup"}],
                        },
                    },
                ]
                write_jsonl(
                    home / ".codex" / "sessions" / "2026" / "05" / "16" / "rollout.jsonl",
                    records,
                )
                write_jsonl(
                    home / ".codex-3" / "sessions" / "duplicate.jsonl",
                    records,
                )

                result = push_backup(
                    home=home,
                    db_url="postgresql://example",
                    storage_config=R2Config(
                        bucket="logpile-raw",
                        endpoint_url="https://example.r2.cloudflarestorage.com",
                    ),
                )
        finally:
            backup.S3ObjectStore = original_store
            backup.SupabaseArchive = original_archive

        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(result["indexed_chunks"], 1)
        self.assertEqual(len(uploaded_keys), 1)
        self.assertEqual(len(upserted_files), 1)
        self.assertEqual(indexed_files, upserted_files)


if __name__ == "__main__":
    unittest.main()
