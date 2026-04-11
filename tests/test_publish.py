import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from logpile.cli import cli
import logpile.db as db_module
from logpile.db import ensure_user, init_db, set_session_visibility, update_user
from logpile.sync import sync_sessions


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record))
            fh.write("\n")


def open_sqlite(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return closing(conn)


class PublishTests(unittest.TestCase):
    def _write_session(
        self,
        home: Path,
        *,
        session_id: str = "session-1",
        body: str = "hello world",
    ) -> Path:
        session_path = (
            home
            / ".claude"
            / "projects"
            / "-Users-alice-demo"
            / f"{session_id}.jsonl"
        )
        write_jsonl(
            session_path,
            [
                {
                    "timestamp": "2026-04-10T10:00:00Z",
                    "type": "user",
                    "cwd": "/tmp/demo",
                    "message": {"content": body},
                },
                {
                    "timestamp": "2026-04-10T10:00:05Z",
                    "type": "assistant",
                    "message": {
                        "id": "msg-1",
                        "model": "claude-3.7",
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                        "content": [{"type": "text", "text": "ack"}],
                    },
                },
            ],
        )
        return session_path

    def _prepare_db(self, db_path: Path) -> None:
        init_db(db_path)
        with open_sqlite(db_path) as conn:
            ensure_user(conn, "alice", display_name="Alice")
            update_user(conn, "alice", default_session_visibility="private")
            conn.commit()

    def test_review_reports_risks_and_recommends_private(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(
                home,
                body=(
                    "Please contact alice@example.com. "
                    "Token sk-ant-abcdefghijklmnopqrstuvwxyz1234567890. "
                    "-----BEGIN OPENSSH PRIVATE KEY-----"
                ),
            )

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                ["publish", "review", "session-1", "--db", str(db_path)],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Recommendation: private", result.output)
            self.assertIn("Email address", result.output)
            self.assertIn("Token or credential", result.output)
            self.assertIn("Private key material", result.output)
            self.assertIn("Inspected file:", result.output)

    def test_publish_queue_lists_pending_sessions_with_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, body="Polish the session index.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                ["publish", "queue", "--db", str(db_path), "--limit", "10"],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("session-1", result.output)
            self.assertIn("summary:", result.output)
            self.assertIn("outcome:", result.output)
            self.assertIn("review: public", result.output)

    def test_review_json_outputs_structured_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, body="Discuss the change with the team.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                ["publish", "review", "session-1", "--db", str(db_path), "--json"],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["session_id"], "session-1")
            self.assertEqual(payload["recommendation"], "public")
            self.assertEqual(payload["current_visibility"], "private")
            self.assertIsInstance(payload["metadata"], dict)
            self.assertIsInstance(payload["findings"], list)

    def test_review_json_reports_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            self._prepare_db(db_path)

            result = CliRunner().invoke(
                cli,
                ["publish", "review", "missing-session", "--db", str(db_path), "--json"],
            )

            self.assertEqual(result.exit_code, 1)
            payload = json.loads(result.output)
            self.assertEqual(payload["error"], "not found")
            self.assertEqual(payload["code"], "not_found")

    def test_approve_sets_public_visibility_for_clean_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, body="Discuss the change with the team.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "approve",
                    "session-1",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                    "--visibility",
                    "public",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Updated 1 session(s) to visibility=public", result.output)

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, visibility_source, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()

            self.assertEqual(row["visibility"], "public")
            self.assertEqual(row["visibility_source"], "manual")
            self.assertTrue(row["shared_path"])
            self.assertTrue(Path(row["shared_path"]).exists())

    def test_apply_blocks_public_publish_when_review_is_risky(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(
                home,
                body=(
                    "Send to alice@example.com and use "
                    "sk-ant-abcdefghijklmnopqrstuvwxyz1234567890."
                ),
            )

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "apply",
                    "session-1",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                    "--visibility",
                    "public",
                ],
            )

            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertIn("Recommendation: private", result.output)
            self.assertIn("refusing to set public", result.output)

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, visibility_source, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()

            self.assertEqual(row["visibility"], "private")
            self.assertEqual(row["visibility_source"], "default")
            self.assertEqual(row["shared_path"], "")
            self.assertFalse((shared / "alice" / "claudecode" / "demo" / "session-1.jsonl").exists())

    def test_review_does_not_flag_local_home_paths_from_metadata_alone(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, body="Clean session.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                conn.execute(
                    """
                    UPDATE sessions
                    SET workspace_root = ?, worktree_root = ?, repo_root = ?
                    WHERE session_id = 'session-1'
                    """,
                    (
                        "/Users/alice/work/logpile",
                        "/Users/alice/work/logpile",
                        "/Users/alice/work/logpile",
                    ),
                )
                conn.commit()

            result = CliRunner().invoke(
                cli,
                ["publish", "review", "session-1", "--db", str(db_path)],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Recommendation: public", result.output)
            self.assertNotIn("Absolute home path", result.output)

    def test_review_prefers_shared_publish_artifact_over_private_source_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            session_path = self._write_session(home, body="Publishable shared artifact.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                set_session_visibility(conn, "session-1", "unlisted", shared_dir=shared)
                conn.commit()

            session_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-10T10:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {
                            "content": "sk-ant-abcdefghijklmnopqrstuvwxyz1234567890"
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                cli,
                ["publish", "review", "session-1", "--db", str(db_path)],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Recommendation: public", result.output)
            self.assertNotIn("Token or credential", result.output)

    def test_approve_keeps_reviewed_shared_artifact_when_promoting_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            session_path = self._write_session(home, body="Publishable shared artifact.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                set_session_visibility(conn, "session-1", "unlisted", shared_dir=shared)
                conn.commit()

            session_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-10T10:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {
                            "content": "sk-ant-abcdefghijklmnopqrstuvwxyz1234567890"
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "approve",
                    "session-1",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                    "--visibility",
                    "public",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            shared_path = shared / "alice" / "claudecode" / "demo" / "session-1.jsonl"
            self.assertTrue(shared_path.exists())
            self.assertNotIn("sk-ant-", shared_path.read_text(encoding="utf-8"))

    def test_approve_keeps_reviewed_source_artifact_on_first_publish(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            session_path = self._write_session(home, body="Publishable source artifact.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            original_set_visibility = db_module.set_session_visibility

            def race_set_visibility(conn, session_id, visibility, *, shared_dir):
                session_path.write_text(
                    json.dumps(
                        {
                            "timestamp": "2026-04-10T10:00:00Z",
                            "type": "user",
                            "cwd": "/tmp/demo",
                            "message": {
                                "content": "sk-ant-abcdefghijklmnopqrstuvwxyz1234567890"
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return original_set_visibility(
                    conn,
                    session_id,
                    visibility,
                    shared_dir=shared_dir,
                )

            with mock.patch("logpile.db.set_session_visibility", side_effect=race_set_visibility):
                result = CliRunner().invoke(
                    cli,
                    [
                        "publish",
                        "approve",
                        "session-1",
                        "--db",
                        str(db_path),
                        "--shared",
                        str(shared),
                        "--visibility",
                        "public",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            shared_path = shared / "alice" / "claudecode" / "demo" / "session-1.jsonl"
            self.assertTrue(shared_path.exists())
            self.assertNotIn("sk-ant-", shared_path.read_text(encoding="utf-8"))

    def test_review_rejects_ambiguous_session_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, session_id="session-alpha", body="one")
            self._write_session(home, session_id="session-alpine", body="two")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                ["publish", "review", "session-al", "--db", str(db_path)],
            )

            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertIn("Ambiguous session id prefix", result.output)

    def test_visibility_command_rejects_ambiguous_session_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, session_id="session-alpha", body="one")
            self._write_session(home, session_id="session-alpine", body="two")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                [
                    "visibility",
                    "session-al",
                    "public",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                ],
            )

            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertIn("Ambiguous session id prefix", result.output)
