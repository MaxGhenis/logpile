import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from logpile.cli import cli
from logpile.db import ensure_user, get_db, init_db


class CliBackendTests(unittest.TestCase):
    def test_lock_contended_sync_exits_with_retryable_status(self) -> None:
        from logpile.sync import SyncLockContended

        with tempfile.TemporaryDirectory() as td, mock.patch(
            "logpile.sync.sync_sessions", return_value=SyncLockContended(0, 0, 0)
        ):
            root = Path(td)
            result = CliRunner().invoke(
                cli,
                [
                    "sync",
                    "--db",
                    str(root / "logpile.db"),
                    "--shared",
                    str(root / "shared"),
                    "--username",
                    "alice",
                ],
            )

        self.assertEqual(result.exit_code, 75, result.output)
        self.assertNotIn("Local done", result.output)

    def test_sync_explicit_username_does_not_adopt_single_existing_user(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            session = (
                home
                / ".claude"
                / "projects"
                / "-Users-bob-demo"
                / "bob-session.jsonl"
            )
            session.parent.mkdir(parents=True)
            session.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-11T10:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "Bob's session"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            db_path = root / "logpile.db"
            shared = root / "shared"
            init_db(db_path)
            with get_db(db_path) as conn:
                ensure_user(conn, "alice")

            with mock.patch("pathlib.Path.home", return_value=home):
                result = CliRunner().invoke(
                    cli,
                    [
                        "sync",
                        "--db",
                        str(db_path),
                        "--shared",
                        str(shared),
                        "--username",
                        "bob",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            with get_db(db_path) as conn:
                row = conn.execute(
                    "SELECT username FROM sessions WHERE session_id = 'bob-session'"
                ).fetchone()
                users = {
                    item[0] for item in conn.execute("SELECT username FROM users")
                }
            self.assertEqual(row["username"], "bob")
            self.assertEqual(users, {"alice", "bob"})

    def test_db_backup_creates_verified_point_in_time_copy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "logpile.db"
            output = root / "backups" / "logpile-snapshot.db"
            init_db(source)
            with get_db(source) as conn:
                conn.execute(
                    "INSERT INTO users (username, created_at, updated_at) VALUES (?, ?, ?)",
                    ("alice", "2026-07-11T00:00:00Z", "2026-07-11T00:00:00Z"),
                )

            result = CliRunner().invoke(
                cli,
                ["db-backup", str(output), "--db", str(source)],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn(f"Verified SQLite backup: {output}", result.output)
            with get_db(output) as conn:
                username = conn.execute("SELECT username FROM users").fetchone()["username"]
                quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]

        self.assertEqual(username, "alice")
        self.assertEqual(quick_check, "ok")

    def test_serve_defaults_to_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            fake_app = mock.Mock()

            with mock.patch("logpile.web.app.create_app", return_value=fake_app):
                result = CliRunner().invoke(
                    cli,
                    ["serve", "--flask", "--db", str(db_path)],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        fake_app.run.assert_called_once_with(host="127.0.0.1", port=5002)

    def test_wheel_serve_fails_with_source_checkout_instructions(self) -> None:
        with mock.patch("logpile.cli._source_checkout_root", return_value=None):
            result = CliRunner().invoke(cli, ["serve"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("requires the Logpile source checkout", result.output)
        self.assertIn("https://github.com/MaxGhenis/logpile", result.output)
        self.assertIn("wheels intentionally do not contain web assets", result.output)
        self.assertIn("sync, stats, search, and db-backup", result.output)
        self.assertNotIn("Database not found", result.output)

    def test_private_serve_refuses_non_loopback_without_override(self) -> None:
        result = CliRunner().invoke(cli, ["serve", "--host", "0.0.0.0"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("may only bind to loopback", result.output)
        self.assertIn("--unsafe-network", result.output)

    def test_private_serve_allows_explicit_unsafe_network_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            fake_app = mock.Mock()

            with mock.patch("logpile.web.app.create_app", return_value=fake_app):
                result = CliRunner().invoke(
                    cli,
                    [
                        "serve",
                        "--flask",
                        "--db",
                        str(db_path),
                        "--host",
                        "0.0.0.0",
                        "--unsafe-network",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        fake_app.run.assert_called_once_with(host="0.0.0.0", port=5002)

    def test_direct_next_serve_reconciles_frozen_lockfile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            process = mock.Mock()
            process.wait.return_value = 0

            with (
                mock.patch("shutil.which", return_value="/fake/bun"),
                mock.patch("subprocess.run") as run,
                mock.patch("subprocess.Popen", return_value=process),
                mock.patch("signal.signal"),
            ):
                result = CliRunner().invoke(
                    cli,
                    ["serve", "--dev", "--db", str(db_path)],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        run.assert_called_once_with(
            ["/fake/bun", "install", "--frozen-lockfile", "--silent"],
            cwd=str(Path(__file__).resolve().parents[1] / "web"),
            check=True,
        )

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
