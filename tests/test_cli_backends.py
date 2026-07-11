import json
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

from logpile.cli import cli
from logpile.db import get_db, init_db


class CliBackendTests(unittest.TestCase):
    def test_cloud_search_uses_supabase_archive(self) -> None:
        import logpile.backup as backup

        class FakeArchive:
            def __init__(self, db_url):
                self.db_url = db_url

            def search(self, query, *, limit=20):
                return [
                    {
                        "session_id": "cloud-session",
                        "relative_path": ".codex/sessions/demo.jsonl",
                        "event_index": 3,
                        "fragment_index": 0,
                        "chunk_index": 0,
                        "role": "user",
                        "excerpt": f"found {query}",
                    }
                ]

        original = backup.SupabaseArchive
        backup.SupabaseArchive = FakeArchive
        try:
            result = CliRunner().invoke(
                cli,
                [
                    "search",
                    "specific thing",
                    "--backend",
                    "cloud",
                    "--db-url",
                    "postgresql://example",
                    "--json",
                ],
            )
        finally:
            backup.SupabaseArchive = original

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["backend"], "cloud")
        self.assertEqual(payload["results"][0]["session_id"], "cloud-session")
        self.assertIn("specific thing", payload["results"][0]["excerpt"])

    def test_cloud_show_prints_indexed_chunks(self) -> None:
        import logpile.backup as backup

        class FakeArchive:
            def __init__(self, db_url):
                self.db_url = db_url

            def session_chunks(self, session_id, *, limit=200):
                return [
                    {
                        "session_id": "sess-1",
                        "relative_path": ".claude/projects/demo/session.jsonl",
                        "event_index": 1,
                        "fragment_index": 0,
                        "chunk_index": 0,
                        "role": "user",
                        "tool_name": None,
                        "content": "find exact raw content",
                    }
                ]

        original = backup.SupabaseArchive
        backup.SupabaseArchive = FakeArchive
        try:
            result = CliRunner().invoke(
                cli,
                [
                    "show",
                    "sess",
                    "--backend",
                    "cloud",
                    "--db-url",
                    "postgresql://example",
                    "--json",
                ],
            )
        finally:
            backup.SupabaseArchive = original

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["backend"], "cloud")
        self.assertEqual(payload["chunks"][0]["content"], "find exact raw content")

    def test_local_search_reads_local_private_store_without_cloud_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            raw_path = root / "session.jsonl"
            raw_path.write_text('{"message": "needle in raw local file"}\n', encoding="utf-8")
            init_db(db_path)
            with get_db(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, source, username, source_path, shared_path,
                        first_timestamp, first_user_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "local-session",
                        "claudecode",
                        "alice",
                        str(raw_path),
                        "",
                        "2026-05-18T12:00:00Z",
                        "metadata does not contain the query",
                    ),
                )

            result = CliRunner().invoke(
                cli,
                [
                    "search",
                    "needle",
                    "--backend",
                    "local",
                    "--db",
                    str(db_path),
                    "--json",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["backend"], "local")
        self.assertEqual(payload["results"][0]["session_id"], "local-session")
        self.assertIn("needle", payload["results"][0]["excerpt"])


if __name__ == "__main__":
    unittest.main()
