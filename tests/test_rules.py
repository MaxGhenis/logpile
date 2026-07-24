import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from click.testing import CliRunner

from logpile.cli import cli
from logpile.db import (
    create_visibility_rule,
    ensure_user,
    init_db,
    recompute_session_visibility,
    set_session_visibility,
    update_user,
)
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


class SessionRuleTests(unittest.TestCase):
    def _assert_private_archive(self, value: str, shared: Path) -> None:
        archive = Path(value)
        self.assertTrue(archive.exists())
        self.assertFalse(archive.is_relative_to(shared))
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

    def _write_claude_session(
        self,
        home: Path,
        *,
        session_id: str = "session-1",
        message: str = "hello world",
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
                    "message": {"content": message},
                },
                {
                    "timestamp": "2026-04-10T10:00:05Z",
                    "type": "assistant",
                    "message": {
                        "id": "msg-1",
                        "model": "claude-3.7",
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                        "content": [{"type": "text", "text": "hi"}],
                    },
                },
            ],
        )
        return session_path

    def test_deterministic_rule_applies_during_sync(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_claude_session(home)

            init_db(db_path)
            with open_sqlite(db_path) as conn:
                ensure_user(conn, "alice", display_name="alice")
                create_visibility_rule(
                    conn,
                    "alice",
                    field="project",
                    match_mode="contains",
                    pattern="demo",
                    visibility="private",
                    source_scope="claudecode",
                )
                conn.commit()

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT visibility, visibility_source, visibility_rule_id, visibility_reason, shared_path
                    FROM sessions
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(row["visibility"], "private")
            self.assertEqual(row["visibility_source"], "rule")
            self.assertIsNotNone(row["visibility_rule_id"])
            self.assertIn("project contains", row["visibility_reason"])
            self._assert_private_archive(row["shared_path"], shared)
            self.assertFalse((shared / "alice" / "claudecode" / "demo" / "session-1.jsonl").exists())

    def test_fuzzy_rule_applies_during_sync(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_claude_session(home, message="hello world")

            init_db(db_path)
            with open_sqlite(db_path) as conn:
                ensure_user(conn, "alice", display_name="alice")
                create_visibility_rule(
                    conn,
                    "alice",
                    field="first_user_message",
                    match_mode="fuzzy",
                    pattern="helo wrld",
                    visibility="unlisted",
                    threshold=0.6,
                    source_scope="claudecode",
                )
                conn.commit()

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT visibility, visibility_source, visibility_reason
                    FROM sessions
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(row["visibility"], "unlisted")
            self.assertEqual(row["visibility_source"], "rule")
            self.assertIn("score=", row["visibility_reason"])

    def test_manual_override_survives_resync_when_rule_matches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            session_path = self._write_claude_session(home)

            init_db(db_path)
            with open_sqlite(db_path) as conn:
                ensure_user(conn, "alice", display_name="alice")
                create_visibility_rule(
                    conn,
                    "alice",
                    field="project",
                    match_mode="contains",
                    pattern="demo",
                    visibility="private",
                )
                conn.commit()

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                with self.assertWarnsRegex(
                    RuntimeWarning, "no publish review was required"
                ):
                    set_session_visibility(
                        conn, "session-1", "unlisted", shared_dir=shared
                    )
                conn.commit()

            with session_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "timestamp": "2026-04-10T10:00:06Z",
                            "type": "assistant",
                            "message": {
                                "id": "msg-2",
                                "model": "claude-3.7",
                                "usage": {"input_tokens": 2, "output_tokens": 3},
                                "content": [{"type": "text", "text": "updated"}],
                            },
                        }
                    )
                )
                fh.write("\n")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT visibility, visibility_source, visibility_rule_id, visibility_reason
                    FROM sessions
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(row["visibility"], "unlisted")
            self.assertEqual(row["visibility_source"], "manual")
            self.assertIsNone(row["visibility_rule_id"])
            self.assertEqual(row["visibility_reason"], "manual override")

    def test_recompute_applies_rule_to_existing_non_manual_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_claude_session(home)

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                create_visibility_rule(
                    conn,
                    "alice",
                    field="project",
                    match_mode="contains",
                    pattern="demo",
                    visibility="unlisted",
                )
                updated = recompute_session_visibility(conn, identifier="alice", shared_dir=shared)
                conn.commit()

                row = conn.execute(
                    """
                    SELECT visibility, visibility_source
                    FROM sessions
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(updated, 1)
            self.assertEqual(row["visibility"], "unlisted")
            self.assertEqual(row["visibility_source"], "rule")

    def test_recompute_private_rule_clears_shared_copy_without_cli(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            session_path = self._write_claude_session(home)

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            copied_path = shared / "alice" / "claudecode" / "demo" / session_path.name
            self.assertTrue(copied_path.exists())

            with open_sqlite(db_path) as conn:
                create_visibility_rule(
                    conn,
                    "alice",
                    field="project",
                    match_mode="contains",
                    pattern="demo",
                    visibility="private",
                )
                updated = recompute_session_visibility(conn, identifier="alice", shared_dir=shared)
                conn.commit()
                row = conn.execute(
                    """
                    SELECT visibility, visibility_source, shared_path
                    FROM sessions
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(updated, 1)
            self.assertFalse(copied_path.exists())
            self.assertEqual(row["visibility"], "private")
            self.assertEqual(row["visibility_source"], "rule")
            self._assert_private_archive(row["shared_path"], shared)

    def test_raw_recompute_rolls_back_storage_when_shared_path_update_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = self._write_claude_session(home)
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            shared_copy = shared / "alice" / "claudecode" / "demo" / source.name
            expected = shared_copy.read_bytes()
            source.unlink()

            with open_sqlite(db_path) as conn:
                create_visibility_rule(
                    conn,
                    "alice",
                    field="project",
                    match_mode="contains",
                    pattern="demo",
                    visibility="private",
                )
                conn.execute(
                    """
                    CREATE TRIGGER reject_shared_path BEFORE UPDATE OF shared_path ON sessions
                    BEGIN SELECT RAISE(ABORT, 'forced shared path failure'); END
                    """
                )
                conn.commit()
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "forced shared path failure"
                ):
                    recompute_session_visibility(
                        conn, identifier="alice", shared_dir=shared
                    )
                conn.rollback()

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, shared_path FROM sessions "
                    "WHERE session_id = 'session-1'"
                ).fetchone()
            self.assertEqual(row["visibility"], "unlisted")
            self.assertEqual(Path(row["shared_path"]), shared_copy)
            self.assertEqual(shared_copy.read_bytes(), expected)
            private_root = root / ".shared-private"
            self.assertFalse(
                private_root.exists() and any(private_root.rglob("*.jsonl"))
            )

    def test_rules_apply_reconciles_shared_copy_for_new_private_rule(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            session_path = self._write_claude_session(home)

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            copied_path = shared / "alice" / "claudecode" / "demo" / session_path.name
            self.assertTrue(copied_path.exists())

            with open_sqlite(db_path) as conn:
                create_visibility_rule(
                    conn,
                    "alice",
                    field="project",
                    match_mode="contains",
                    pattern="demo",
                    visibility="private",
                )
                conn.commit()

            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "rules",
                    "apply",
                    "--user",
                    "alice",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertFalse(copied_path.exists())

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT visibility, visibility_source, shared_path
                    FROM sessions
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(row["visibility"], "private")
            self.assertEqual(row["visibility_source"], "rule")
            self._assert_private_archive(row["shared_path"], shared)

    def test_rules_apply_with_unknown_user_exits_without_touching_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            session_path = self._write_claude_session(home)

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            copied_path = shared / "alice" / "claudecode" / "demo" / session_path.name
            self.assertTrue(copied_path.exists())

            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "rules",
                    "apply",
                    "--user",
                    "missing-user",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("No user found matching 'missing-user'", result.output)
            self.assertTrue(copied_path.exists())

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT visibility, visibility_source, shared_path
                    FROM sessions
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(row["visibility"], "unlisted")
            self.assertEqual(row["visibility_source"], "default")
            self.assertEqual(row["shared_path"], str(copied_path))

    def test_rules_delete_recomputes_and_reconciles_storage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            session_path = self._write_claude_session(home)

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            copied_path = shared / "alice" / "claudecode" / "demo" / session_path.name
            self.assertTrue(copied_path.exists())

            with open_sqlite(db_path) as conn:
                update_user(conn, "alice", default_session_visibility="private")
                rule = create_visibility_rule(
                    conn,
                    "alice",
                    field="project",
                    match_mode="contains",
                    pattern="demo",
                    visibility="public",
                )
                with self.assertWarnsRegex(RuntimeWarning, "kept this session unlisted"):
                    updated = recompute_session_visibility(
                        conn, identifier="alice", shared_dir=shared
                    )
                conn.commit()
                self.assertEqual(updated, 1)

            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "rules",
                    "delete",
                    str(rule["id"]),
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertFalse(copied_path.exists())

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT visibility, visibility_source, shared_path
                    FROM sessions
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(row["visibility"], "private")
            self.assertEqual(row["visibility_source"], "default")
            self._assert_private_archive(row["shared_path"], shared)


if __name__ == "__main__":
    unittest.main()
