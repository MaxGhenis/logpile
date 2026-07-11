import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

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


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record))
            fh.write("\n")


class BackupTests(unittest.TestCase):
    def test_plan_discovers_agent_logs_and_codex_db_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            write_jsonl(
                home / ".codex" / "sessions" / "2026" / "05" / "16" / "rollout.jsonl",
                [
                    {
                        "type": "session_meta",
                        "payload": {"id": "codex-1", "timestamp": "2026-05-16T10:00:00Z"},
                    }
                ],
            )
            write_jsonl(
                home / ".codex" / "archived_sessions" / "old.jsonl",
                [{"type": "session_meta", "payload": {"id": "archive-1"}}],
            )
            write_jsonl(
                home / ".claude" / "projects" / "-tmp-project" / "claude.jsonl",
                [{"type": "user", "sessionId": "claude-1", "message": {"content": "hi"}}],
            )
            codex_db = home / ".codex" / "logs_2.sqlite"
            codex_db.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(codex_db) as conn:
                conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")

            plan = plan_backup(home=home)

            self.assertEqual(len(plan.candidates), 4)
            self.assertEqual(plan.source_counts["codex"], 1)
            self.assertEqual(plan.source_counts["codex_archive"], 1)
            self.assertEqual(plan.source_counts["claudecode"], 1)
            self.assertEqual(plan.source_counts["codex_db"], 1)
            self.assertTrue(all(len(candidate.sha256) == 64 for candidate in plan.candidates))
            self.assertTrue(all(candidate.object_key.startswith("raw/sha256/") for candidate in plan.candidates))

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
                write_jsonl(
                    home / ".codex" / "sessions" / "2026" / "05" / "16" / "rollout.jsonl",
                    [
                        {"type": "session_meta", "payload": {"id": "push-1"}},
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "find raw log cloud backup"}],
                            },
                        },
                    ],
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
