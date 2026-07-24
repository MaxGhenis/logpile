import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

import logpile.db as db_module
from logpile.cli import cli
from logpile.db import (
    ensure_user,
    get_db,
    init_db,
    set_session_visibility,
    update_user,
)
from logpile.origins import derive_session_origin
from logpile.sync import (
    SESSION_IDENTITY_VERSION,
    SESSION_TOKEN_VERSION,
    sync_sessions,
)
from logpile.web.app import create_app


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record))
            fh.write("\n")


FIXTURES = Path(__file__).parent / "fixtures"


def open_sqlite(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return closing(conn)


class SyncTests(unittest.TestCase):
    def _assert_private_archive(self, value: str, shared: Path) -> Path:
        archive = Path(value)
        self.assertTrue(archive.exists())
        self.assertFalse(archive.is_symlink())
        self.assertFalse(archive.is_relative_to(shared))
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        return archive

    def _write_claude_session(
        self,
        home: Path,
        *,
        session_id: str = "session-1",
        message: str = "hello world",
        assistant_content: list[dict] | None = None,
        cwd: str = "/tmp/demo",
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
                    "cwd": cwd,
                    "message": {"content": message},
                },
                {
                    "timestamp": "2026-04-10T10:00:05Z",
                    "type": "assistant",
                    "message": {
                        "id": "msg-1",
                        "model": "claude-3.7",
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                        "content": assistant_content or [{"type": "text", "text": "hi"}],
                    },
                },
            ],
        )
        return session_path

    def _init_git_repo(self, root: Path) -> tuple[Path, str, str]:
        repo = root / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "tests@example.com"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Logpile Tests"],
            check=True,
            capture_output=True,
            text=True,
        )
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "init"],
            check=True,
            capture_output=True,
            text=True,
        )
        branch = subprocess.run(
            ["git", "-C", str(repo), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return repo, branch, commit

    def _write_codex_session(
        self,
        home: Path,
        *,
        session_id: str = "rollout-1",
        message: str = "Fix the parser",
        cwd: str = "/tmp/demo",
        total_input_tokens: int = 1200,
        cached_input_tokens: int = 300,
        total_output_tokens: int = 45,
    ) -> Path:
        session_path = (
            home
            / ".codex"
            / "sessions"
            / "2026"
            / "04"
            / "10"
            / f"{session_id}.jsonl"
        )
        write_jsonl(
            session_path,
            [
                {
                    "timestamp": "2026-04-10T10:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "timestamp": "2026-04-10T10:00:00Z",
                        "cwd": cwd,
                    },
                },
                {
                    "timestamp": "2026-04-10T10:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": message}],
                    },
                },
                {
                    "timestamp": "2026-04-10T10:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": total_input_tokens,
                                "cached_input_tokens": cached_input_tokens,
                                "output_tokens": total_output_tokens,
                                "total_tokens": total_input_tokens + total_output_tokens,
                            }
                        },
                    },
                },
            ],
        )
        return session_path

    def test_derive_session_origin_classifies_common_workflow_types(self) -> None:
        self.assertEqual(
            derive_session_origin(
                source="codex",
                session_id="rollout-1",
                first_user_message="make more progress on the cosilico microplex repo (local)",
            )["session_origin"],
            "human_direct",
        )
        self.assertEqual(
            derive_session_origin(
                source="claudecode",
                session_id="00413d2f",
                first_user_message=(
                    "You are a senior statutory-fidelity reviewer for RAC (Rules as Code) "
                    "encodings.\n\nReview the file holistically for citation fidelity."
                ),
                source_path="/private/tmp/autorac-0.2.13-eval/file.jsonl",
            )["session_origin"],
            "pipeline_eval",
        )
        self.assertEqual(
            derive_session_origin(
                source="claudecode",
                session_id="agent-a123456",
                first_user_message=(
                    '<teammate-message teammate_id="team-lead">'
                    '{"type":"task_assignment","subject":"Fix CI"}'
                ),
            )["session_origin"],
            "human_delegated",
        )
        self.assertEqual(
            derive_session_origin(
                source="claudecode",
                session_id="agent-a39249b",
                first_user_message="Warmup",
            )["session_origin"],
            "meta_scaffolding",
        )
        self.assertEqual(
            derive_session_origin(
                source="codex",
                session_id="rollout-2",
                first_user_message="# AGENTS.md instructions for /Users/maxghenis/policyengine-uk",
            )["session_origin"],
            "system_generated",
        )

    def test_sync_copies_files_and_preserves_privacy_on_resync(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            session_path = self._write_claude_session(home)

            new_count, updated_count, skipped_count = sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            self.assertEqual((new_count, updated_count, skipped_count), (1, 0, 0))

            copied_path = shared / "alice" / "claudecode" / "demo" / session_path.name
            self.assertTrue(copied_path.exists())
            self.assertFalse(copied_path.is_symlink())

            with open_sqlite(db_path) as conn:
                set_session_visibility(conn, "session-1", "private", shared_dir=shared)
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

            new_count, updated_count, skipped_count = sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            self.assertEqual((new_count, updated_count), (0, 1))

            with open_sqlite(db_path) as conn:
                visibility, is_private, shared_path = conn.execute(
                    "SELECT visibility, is_private, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()
            self.assertEqual(visibility, "private")
            self.assertEqual(is_private, 1)
            self._assert_private_archive(shared_path, shared)
            self.assertFalse(copied_path.exists())

    def test_public_source_drift_with_restored_size_and_mtime_is_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            session_path = self._write_claude_session(
                home,
                assistant_content=[{"type": "text", "text": "hi"}],
            )

            sync_sessions(shared, db_path, "alice", "machine-1", home)
            approved = CliRunner().invoke(
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
            self.assertEqual(approved.exit_code, 0, approved.output)

            before_stat = session_path.stat()
            with open_sqlite(db_path) as conn:
                before = conn.execute(
                    """
                    SELECT file_hash, reviewed_artifact_path
                    FROM sessions WHERE session_id = 'session-1'
                    """
                ).fetchone()
            reviewed_bytes = Path(before["reviewed_artifact_path"]).read_bytes()

            self._write_claude_session(
                home,
                assistant_content=[{"type": "text", "text": "yo"}],
            )
            self.assertEqual(session_path.stat().st_size, before_stat.st_size)
            os.utime(
                session_path,
                ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns),
            )

            result = sync_sessions(shared, db_path, "alice", "machine-1", home)

            self.assertEqual(result.updated, 1)
            with open_sqlite(db_path) as conn:
                after = conn.execute(
                    """
                    SELECT visibility, publication_state, file_hash,
                           reviewed_artifact_path
                    FROM sessions WHERE session_id = 'session-1'
                    """
                ).fetchone()
            self.assertEqual(after["visibility"], "unlisted")
            self.assertEqual(after["publication_state"], "source_drift")
            self.assertNotEqual(after["file_hash"], before["file_hash"])
            self.assertEqual(
                Path(after["reviewed_artifact_path"]).read_bytes(),
                reviewed_bytes,
            )

    def test_malformed_bound_fields_do_not_abort_later_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            project = home / ".claude" / "projects" / "-Users-alice-demo"
            malformed = project / "aaa-malformed.jsonl"
            write_jsonl(
                malformed,
                [
                    {
                        "timestamp": "2026-07-11T10:00:00Z",
                        "type": "user",
                        "cwd": ["bad"],
                        "message": {"content": "unsafe workspace"},
                    },
                    {
                        "timestamp": "2026-07-11T10:00:01Z",
                        "type": "assistant",
                        "message": {
                            "id": "bad-model",
                            "model": {"bad": 1},
                            "content": [],
                        },
                    },
                ],
            )
            self._write_claude_session(home, session_id="zzz-valid")
            db_path = root / "logpile.db"

            result = sync_sessions(
                root / "shared", db_path, "alice", "machine-1", home
            )

            self.assertEqual((result.new, result.skipped), (1, 1))
            with open_sqlite(db_path) as conn:
                session_ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT session_id FROM sessions ORDER BY session_id"
                    )
                ]
            self.assertEqual(session_ids, ["zzz-valid"])

    def test_manual_private_reconciles_shared_copy_even_if_file_is_unchanged(self) -> None:
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
                set_session_visibility(conn, "session-1", "private", shared_dir=shared)
                conn.commit()

            new_count, updated_count, skipped_count = sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            self.assertEqual((new_count, updated_count, skipped_count), (0, 0, 1))
            self.assertFalse(copied_path.exists())

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()

            self.assertEqual(row["visibility"], "private")
            self._assert_private_archive(row["shared_path"], shared)

    def test_set_session_visibility_clears_shared_copy_without_cli(self) -> None:
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
                count = set_session_visibility(conn, "session-1", "private", shared_dir=shared)
                conn.commit()

            self.assertEqual(count, 1)
            self.assertFalse(copied_path.exists())

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()

            self.assertEqual(row["visibility"], "private")
            self._assert_private_archive(row["shared_path"], shared)

    def test_visibility_command_reconciles_shared_copy_immediately(self) -> None:
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
                    "visibility",
                    "session-1",
                    "private",
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
                    "SELECT visibility, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()

            self.assertEqual(row["visibility"], "private")
            self._assert_private_archive(row["shared_path"], shared)

    def test_private_transition_moves_rotated_only_transcript_to_private_archive(self) -> None:
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
                count = set_session_visibility(
                    conn, "session-1", "private", shared_dir=shared
                )
                conn.commit()
                row = conn.execute(
                    "SELECT visibility, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()

            self.assertEqual(count, 1)
            self.assertFalse(shared_copy.exists())
            archive = self._assert_private_archive(row["shared_path"], shared)
            self.assertEqual(archive.read_bytes(), expected)

    def test_private_transition_rolls_storage_back_when_db_update_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = self._write_claude_session(home)
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            shared_copy = shared / "alice" / "claudecode" / "demo" / source.name
            expected = shared_copy.read_bytes()

            with open_sqlite(db_path) as conn:
                conn.execute(
                    """
                    CREATE TRIGGER reject_private BEFORE UPDATE OF visibility ON sessions
                    WHEN NEW.visibility = 'private'
                    BEGIN SELECT RAISE(ABORT, 'forced visibility failure'); END
                    """
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    set_session_visibility(
                        conn, "session-1", "private", shared_dir=shared
                    )
                row = conn.execute(
                    "SELECT visibility, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()

            self.assertEqual(row["visibility"], "unlisted")
            self.assertEqual(Path(row["shared_path"]), shared_copy)
            self.assertEqual(shared_copy.read_bytes(), expected)
            private_root = root / ".shared-private"
            self.assertFalse(private_root.exists() and any(private_root.rglob("*.jsonl")))

    def test_private_transition_rolls_storage_back_on_explicit_db_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = self._write_claude_session(home)
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            shared_copy = shared / "alice" / "claudecode" / "demo" / source.name
            expected = shared_copy.read_bytes()

            with get_db(db_path) as conn:
                set_session_visibility(
                    conn, "session-1", "private", shared_dir=shared
                )
                archive = Path(
                    conn.execute(
                        "SELECT shared_path FROM sessions WHERE session_id = 'session-1'"
                    ).fetchone()[0]
                )
                self.assertTrue(archive.exists())
                self.assertFalse(shared_copy.exists())
                conn.rollback()

                row = conn.execute(
                    "SELECT visibility, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()
                self.assertEqual(row["visibility"], "unlisted")
                self.assertEqual(Path(row["shared_path"]), shared_copy)

            self.assertEqual(shared_copy.read_bytes(), expected)
            self.assertFalse(archive.exists())

    def test_private_transition_rolls_storage_back_when_db_commit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = self._write_claude_session(home)
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            shared_copy = shared / "alice" / "claudecode" / "demo" / source.name
            expected = shared_copy.read_bytes()

            with (
                mock.patch.object(
                    db_module._StorageTransactionConnection,
                    "_commit_database",
                    side_effect=sqlite3.OperationalError("forced commit failure"),
                ),
                self.assertRaisesRegex(
                    sqlite3.OperationalError, "forced commit failure"
                ),
                get_db(db_path) as conn,
            ):
                set_session_visibility(
                    conn, "session-1", "private", shared_dir=shared
                )
                archive = Path(
                    conn.execute(
                        "SELECT shared_path FROM sessions WHERE session_id = 'session-1'"
                    ).fetchone()[0]
                )

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()
            self.assertEqual(row["visibility"], "unlisted")
            self.assertEqual(Path(row["shared_path"]), shared_copy)
            self.assertEqual(shared_copy.read_bytes(), expected)
            self.assertFalse(archive.exists())

    def test_private_marker_storage_rolls_back_after_late_sync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = self._write_claude_session(home)
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            shared_copy = shared / "alice" / "claudecode" / "demo" / source.name
            expected = shared_copy.read_bytes()
            with source.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "timestamp": "2026-04-10T10:00:06Z",
                            "type": "user",
                            "message": {"content": "# logpile:private"},
                        }
                    )
                    + "\n"
                )

            with mock.patch(
                "logpile.sync.refresh_native_usage",
                side_effect=RuntimeError("forced late sync failure"),
            ), self.assertRaisesRegex(
                RuntimeError, "forced late sync failure"
            ):
                sync_sessions(shared, db_path, "alice", "machine-1", home)

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()
            self.assertEqual(row["visibility"], "unlisted")
            self.assertEqual(Path(row["shared_path"]), shared_copy)
            self.assertEqual(shared_copy.read_bytes(), expected)
            private_root = root / ".shared-private"
            self.assertFalse(
                private_root.exists() and any(private_root.rglob("*.jsonl"))
            )

    def test_private_archive_rejects_symlink_and_non_directory_components(self) -> None:
        attacks = ("root-symlink", "nested-symlink", "nested-file")
        for attack in attacks:
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                home = root / "home"
                shared = root / "shared"
                db_path = root / "logpile.db"
                source = self._write_claude_session(home)
                sync_sessions(shared, db_path, "alice", "machine-1", home)
                shared_copy = (
                    shared / "alice" / "claudecode" / "demo" / source.name
                )
                expected = shared_copy.read_bytes()
                private_root = root / ".shared-private"
                # Sync eagerly claims the private root; the attacker removes
                # and replaces it before the visibility transition runs.
                shutil.rmtree(private_root)
                attacker = root / "attacker"
                attacker.mkdir()

                if attack == "root-symlink":
                    private_root.symlink_to(attacker, target_is_directory=True)
                else:
                    private_root.mkdir(mode=0o700)
                    nested = private_root / "alice"
                    if attack == "nested-symlink":
                        nested.symlink_to(attacker, target_is_directory=True)
                    else:
                        nested.write_text("not a directory", encoding="utf-8")

                with get_db(db_path) as conn, self.assertRaisesRegex(
                    _sync_module.StorageSafetyError,
                    "private archive component",
                ):
                    set_session_visibility(
                        conn, "session-1", "private", shared_dir=shared
                    )

                with open_sqlite(db_path) as conn:
                    row = conn.execute(
                        "SELECT visibility, shared_path FROM sessions WHERE session_id = 'session-1'"
                    ).fetchone()
                self.assertEqual(row["visibility"], "unlisted")
                self.assertEqual(Path(row["shared_path"]), shared_copy)
                self.assertEqual(shared_copy.read_bytes(), expected)
                self.assertFalse(any(attacker.iterdir()))

    def test_private_archive_can_be_promoted_when_source_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = self._write_claude_session(home)
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            source.unlink()

            with open_sqlite(db_path) as conn:
                set_session_visibility(conn, "session-1", "private", shared_dir=shared)
                private_path = Path(
                    conn.execute(
                        "SELECT shared_path FROM sessions WHERE session_id = 'session-1'"
                    ).fetchone()[0]
                )
                with self.assertWarnsRegex(
                    RuntimeWarning, "no publish review was required"
                ):
                    set_session_visibility(
                        conn, "session-1", "unlisted", shared_dir=shared
                    )
                conn.commit()
                row = conn.execute(
                    "SELECT visibility, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()

            public_copy = Path(row["shared_path"])
            self.assertEqual(row["visibility"], "unlisted")
            self.assertTrue(public_copy.is_relative_to(shared))
            self.assertTrue(public_copy.exists())
            self.assertFalse(private_path.exists())

    def test_multiple_visibility_changes_keep_final_private_archive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = self._write_claude_session(home)
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            expected = source.read_bytes()
            source.unlink()

            with get_db(db_path) as conn:
                set_session_visibility(
                    conn, "session-1", "private", shared_dir=shared
                )

            with get_db(db_path) as conn:
                with self.assertWarnsRegex(
                    RuntimeWarning, "no publish review was required"
                ):
                    set_session_visibility(
                        conn, "session-1", "unlisted", shared_dir=shared
                    )
                set_session_visibility(
                    conn, "session-1", "private", shared_dir=shared
                )

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, shared_path FROM sessions "
                    "WHERE session_id = 'session-1'"
                ).fetchone()
            archive = self._assert_private_archive(row["shared_path"], shared)
            self.assertEqual(row["visibility"], "private")
            self.assertEqual(archive.read_bytes(), expected)
            self.assertFalse(
                (shared / "alice" / "claudecode" / "demo" / source.name).exists()
            )

    def test_failed_cleanup_never_leaves_rollback_artifact_in_shared_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = self._write_claude_session(home)
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            original = source.read_bytes()
            with source.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "timestamp": "2026-04-10T10:00:06Z",
                            "type": "user",
                            "message": {"content": "updated transcript"},
                        }
                    )
                    + "\n"
                )

            real_unlink = Path.unlink

            def fail_rollback_cleanup(path, *args, **kwargs):
                if path.name.endswith("publish-rollback"):
                    raise OSError("forced private cleanup failure")
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(
                Path, "unlink", autospec=True, side_effect=fail_rollback_cleanup
            ):
                sync_sessions(shared, db_path, "alice", "machine-1", home)

            private_root = root / ".shared-private"
            leftovers = list(private_root.rglob("*publish-rollback"))
            self.assertEqual(len(leftovers), 1)
            self.assertEqual(leftovers[0].read_bytes(), original)
            self.assertEqual(list(shared.rglob("*publish-rollback")), [])

            with source.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "timestamp": "2026-04-10T10:00:07Z",
                            "type": "user",
                            "message": {"content": "# logpile:private"},
                        }
                    )
                    + "\n"
                )
            sync_sessions(shared, db_path, "alice", "machine-1", home)

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, shared_path FROM sessions "
                    "WHERE session_id = 'session-1'"
                ).fetchone()
            self.assertEqual(row["visibility"], "private")
            self._assert_private_archive(row["shared_path"], shared)
            shared_files = [
                path
                for path in shared.rglob("*")
                if path.is_file() or path.is_symlink()
            ]
            self.assertEqual(shared_files, [])

    def test_failed_promotion_quarantines_shared_copy_if_unlink_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = self._write_claude_session(home)
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            source.unlink()
            with get_db(db_path) as conn:
                set_session_visibility(
                    conn, "session-1", "private", shared_dir=shared
                )
            with open_sqlite(db_path) as conn:
                archive = Path(
                    conn.execute(
                        "SELECT shared_path FROM sessions WHERE session_id = 'session-1'"
                    ).fetchone()[0]
                )

            shared_copy = shared / "alice" / "claudecode" / "demo" / source.name
            real_unlink = Path.unlink

            def fail_shared_unlink(path, *args, **kwargs):
                if path == shared_copy:
                    raise OSError("forced shared unlink failure")
                return real_unlink(path, *args, **kwargs)

            with get_db(db_path) as conn:
                conn.execute(
                    """
                    CREATE TRIGGER reject_promotion BEFORE UPDATE OF visibility ON sessions
                    WHEN NEW.visibility != 'private'
                    BEGIN SELECT RAISE(ABORT, 'forced promotion failure'); END
                    """
                )
                with mock.patch.object(
                    Path, "unlink", autospec=True, side_effect=fail_shared_unlink
                ), self.assertRaisesRegex(
                    sqlite3.IntegrityError, "forced promotion failure"
                ):
                    set_session_visibility(
                        conn, "session-1", "unlisted", shared_dir=shared
                    )

            self.assertFalse(shared_copy.exists())
            self.assertTrue(archive.exists())
            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, shared_path FROM sessions "
                    "WHERE session_id = 'session-1'"
                ).fetchone()
            self.assertEqual(row["visibility"], "private")
            self.assertEqual(Path(row["shared_path"]), archive)

    def test_private_transition_rejects_nonregular_only_shared_copy(self) -> None:
        for kind in ("directory", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                home = root / "home"
                shared = root / "shared"
                db_path = root / "logpile.db"
                source = self._write_claude_session(home)
                sync_sessions(shared, db_path, "alice", "machine-1", home)
                shared_copy = (
                    shared / "alice" / "claudecode" / "demo" / source.name
                )
                source.unlink()
                shared_copy.unlink()
                if kind == "directory":
                    shared_copy.mkdir()
                else:
                    os.mkfifo(shared_copy)

                with get_db(db_path) as conn, self.assertRaisesRegex(
                    _sync_module.StorageSafetyError,
                    "non-regular or unsafe shared transcript",
                ):
                    set_session_visibility(
                        conn, "session-1", "private", shared_dir=shared
                    )

                self.assertTrue(shared_copy.exists())
                with open_sqlite(db_path) as conn:
                    visibility = conn.execute(
                        "SELECT visibility FROM sessions WHERE session_id = 'session-1'"
                    ).fetchone()[0]
                self.assertEqual(visibility, "unlisted")

    def test_sync_and_private_transition_reject_symlinked_shared_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            outside = root / "outside"
            outside.mkdir()
            shared.symlink_to(outside, target_is_directory=True)
            self._write_claude_session(home)

            with self.assertRaisesRegex(
                _sync_module.StorageSafetyError, "symlink shared storage component"
            ):
                sync_sessions(
                    shared, root / "symlink.db", "alice", "machine-1", home
                )
            self.assertEqual(list(outside.iterdir()), [])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = self._write_claude_session(home)
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            real_user_dir = root / "real-user-dir"
            user_dir = shared / "alice"
            user_dir.rename(real_user_dir)
            user_dir.symlink_to(real_user_dir, target_is_directory=True)

            with get_db(db_path) as conn, self.assertRaisesRegex(
                _sync_module.StorageSafetyError,
                "non-regular or unsafe shared transcript",
            ):
                set_session_visibility(
                    conn, "session-1", "private", shared_dir=shared
                )

            outside_copy = real_user_dir / "claudecode" / "demo" / source.name
            self.assertTrue(outside_copy.exists())

    def test_reconcile_attempts_every_rollback_after_one_rollback_fails(self) -> None:
        rows = [
            {
                "session_id": session_id,
                "username": "alice",
                "source": "claudecode",
                "project": "demo",
                "source_path": f"/missing/{session_id}.jsonl",
                "shared_path": f"/shared/{session_id}.jsonl",
                "visibility": "private",
            }
            for session_id in ("first", "second")
        ]
        cursor = mock.Mock()
        cursor.fetchall.return_value = rows
        conn = mock.Mock()
        update_count = 0

        def execute(sql, *_args):
            nonlocal update_count
            if "SELECT" in sql:
                return cursor
            update_count += 1
            if update_count == 2:
                raise sqlite3.IntegrityError("forced second update failure")
            return mock.Mock()

        conn.execute.side_effect = execute
        first = mock.Mock(
            archive_path=Path("/private/first.jsonl"), changed=True
        )
        second = mock.Mock(
            archive_path=Path("/private/second.jsonl"), changed=True
        )
        second.rollback.side_effect = _sync_module.StorageSafetyError(
            "forced rollback failure"
        )

        with mock.patch(
            "logpile.sync._prepare_sync_shared_copy",
            side_effect=[first, second],
        ), self.assertRaisesRegex(
            _sync_module.StorageSafetyError, "forced rollback failure"
        ):
            _sync_module.reconcile_session_storage(
                conn, shared_dir=Path("/shared")
            )

        second.rollback.assert_called_once_with()
        first.rollback.assert_called_once_with()

    def test_private_transition_refuses_when_no_transcript_survives(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = self._write_claude_session(home)
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with open_sqlite(db_path) as conn:
                shared_copy = Path(
                    conn.execute(
                        "SELECT shared_path FROM sessions WHERE session_id = 'session-1'"
                    ).fetchone()[0]
                )
            source.unlink()
            shared_copy.unlink()

            with open_sqlite(db_path) as conn:
                with self.assertRaisesRegex(
                    _sync_module.StorageSafetyError,
                    "no source or shared transcript survives",
                ):
                    set_session_visibility(
                        conn, "session-1", "private", shared_dir=shared
                    )
                visibility = conn.execute(
                    "SELECT visibility FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()[0]

            self.assertEqual(visibility, "unlisted")

            result = CliRunner().invoke(
                cli,
                [
                    "private",
                    "session-1",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                ],
            )
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn(
                "no source or shared transcript survives", result.output
            )

    def test_private_marker_tightens_existing_claude_and_codex_rows(self) -> None:
        for source_name in ("claudecode", "codex"):
            with self.subTest(source=source_name), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                home = root / "home"
                shared = root / "shared"
                db_path = root / "logpile.db"
                if source_name == "claudecode":
                    path = self._write_claude_session(home, session_id="marker-session")
                else:
                    path = self._write_codex_session(home, session_id="marker-session")
                sync_sessions(shared, db_path, "alice", "machine-1", home)
                with open_sqlite(db_path) as conn:
                    old_shared = Path(
                        conn.execute(
                            "SELECT shared_path FROM sessions WHERE session_id = 'marker-session'"
                        ).fetchone()[0]
                    )

                with path.open("a", encoding="utf-8") as stream:
                    if source_name == "claudecode":
                        stream.write(
                            json.dumps(
                                {
                                    "timestamp": "2026-04-10T10:00:06Z",
                                    "type": "user",
                                    "message": {"content": "# logpile:private"},
                                }
                            )
                            + "\n"
                        )
                    else:
                        stream.write(
                            json.dumps(
                                {
                                    "timestamp": "2026-04-10T10:00:06Z",
                                    "type": "response_item",
                                    "payload": {
                                        "type": "message",
                                        "role": "user",
                                        "content": [
                                            {"type": "input_text", "text": "# logpile:private"}
                                        ],
                                    },
                                }
                            )
                            + "\n"
                        )

                result = sync_sessions(shared, db_path, "alice", "machine-1", home)
                with open_sqlite(db_path) as conn:
                    row = conn.execute(
                        """
                        SELECT visibility, visibility_source, visibility_reason, shared_path
                        FROM sessions WHERE session_id = 'marker-session'
                        """
                    ).fetchone()

                self.assertEqual(result.updated, 1)
                self.assertEqual(row["visibility"], "private")
                self.assertEqual(row["visibility_source"], "marker")
                self.assertIn("logpile:private", row["visibility_reason"])
                self.assertFalse(old_shared.exists())
                archive = self._assert_private_archive(row["shared_path"], shared)
                self.assertIn(b"logpile:private", archive.read_bytes())

    def test_sequential_syncs_keep_distinct_users(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            shared = root / "shared"
            alice_home = root / "alice-home"
            bob_home = root / "bob-home"
            self._write_claude_session(
                alice_home, session_id="alice-session", message="Alice"
            )
            self._write_claude_session(
                bob_home, session_id="bob-session", message="Bob"
            )

            sync_sessions(shared, db_path, "alice", "alice-machine", alice_home)
            sync_sessions(shared, db_path, "bob", "bob-machine", bob_home)

            with open_sqlite(db_path) as conn:
                rows = conn.execute(
                    "SELECT session_id, username FROM sessions ORDER BY session_id"
                ).fetchall()
            self.assertEqual(
                [(row["session_id"], row["username"]) for row in rows],
                [("alice-session", "alice"), ("bob-session", "bob")],
            )

    def test_sync_populates_workspace_root_and_session_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_claude_session(
                home,
                assistant_content=[
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "id": "tool-1",
                        "input": {"file_path": "src/app.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": "tool-2",
                        "input": {
                            "command": "rg -n session_paths src/app.py tests/test_sync.py"
                        },
                    },
                ],
            )

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                session = conn.execute(
                    "SELECT project, workspace_root FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()
                paths = conn.execute(
                    """
                    SELECT display_path, operation, source, occurrence_count
                    FROM session_paths
                    WHERE session_id = 'session-1'
                    ORDER BY display_path, source
                    """
                ).fetchall()

            self.assertEqual(session["project"], "demo")
            self.assertEqual(session["workspace_root"], "/tmp/demo")
            self.assertEqual(
                [(row["display_path"], row["operation"], row["source"], row["occurrence_count"]) for row in paths],
                [
                    ("src/app.py", "search", "command", 1),
                    ("src/app.py", "write", "tool_input", 1),
                    ("tests/test_sync.py", "search", "command", 1),
                ],
            )

    def test_sync_backfills_codex_tokens_on_unchanged_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_codex_session(home, session_id="rollout-token")

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
                    SET total_input_tokens = 0,
                        total_output_tokens = 0,
                        fresh_input_tokens = 0,
                        cached_input_tokens = 0,
                        token_version = 0
                    WHERE session_id = 'rollout-token'
                    """
                )
                conn.commit()

            new_count, updated_count, skipped_count = sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            self.assertEqual((new_count, updated_count, skipped_count), (0, 1, 0))

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT total_input_tokens, total_output_tokens,
                           fresh_input_tokens, cached_input_tokens, token_version
                    FROM sessions
                    WHERE session_id = 'rollout-token'
                    """
                ).fetchone()

            self.assertEqual(row["total_input_tokens"], 1200)
            self.assertEqual(row["total_output_tokens"], 45)
            self.assertEqual(row["fresh_input_tokens"], 900)
            self.assertEqual(row["cached_input_tokens"], 300)
            self.assertEqual(row["token_version"], SESSION_TOKEN_VERSION)

    def test_sync_populates_git_repo_metadata_and_root_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            repo, branch, commit = self._init_git_repo(root)
            workspace = repo / "packages"
            workspace.mkdir(parents=True, exist_ok=True)
            self._write_claude_session(
                home,
                cwd=str(workspace),
                assistant_content=[
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "id": "tool-1",
                        "input": {"file_path": "src/app.py"},
                    }
                ],
            )

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                session = conn.execute(
                    """
                    SELECT
                        workspace_root,
                        worktree_root,
                        repo_root,
                        repo_name,
                        git_branch,
                        git_commit,
                        git_dirty
                    FROM sessions
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()
                path_row = conn.execute(
                    """
                    SELECT display_path, relative_path, repo_relative_path
                    FROM session_paths
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(session["workspace_root"], str(workspace.resolve()))
            self.assertEqual(session["worktree_root"], str(repo.resolve()))
            self.assertEqual(session["repo_root"], str(repo.resolve()))
            self.assertEqual(session["repo_name"], repo.name)
            self.assertEqual(session["git_branch"], branch)
            self.assertEqual(session["git_commit"], commit)
            self.assertEqual(session["git_dirty"], 0)
            self.assertEqual(path_row["display_path"], "src/app.py")
            self.assertEqual(path_row["relative_path"], "src/app.py")
            self.assertEqual(path_row["repo_relative_path"], "packages/src/app.py")

    def test_sync_canonicalizes_repo_root_across_git_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            repo, _, commit = self._init_git_repo(root)
            worktree = root / "repo-feature"
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "-b", "feature/logpile", str(worktree), "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            )
            workspace = worktree / "src"
            workspace.mkdir(parents=True, exist_ok=True)
            self._write_claude_session(
                home,
                cwd=str(workspace),
                assistant_content=[
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "id": "tool-1",
                        "input": {"file_path": "app.py"},
                    }
                ],
            )

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                session = conn.execute(
                    """
                    SELECT worktree_root, repo_root, repo_name, git_branch, git_commit
                    FROM sessions
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()
                path_row = conn.execute(
                    """
                    SELECT relative_path, repo_relative_path
                    FROM session_paths
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(session["worktree_root"], str(worktree.resolve()))
            self.assertEqual(session["repo_root"], str(repo.resolve()))
            self.assertEqual(session["repo_name"], repo.name)
            self.assertEqual(session["git_branch"], "feature/logpile")
            self.assertEqual(session["git_commit"], commit)
            self.assertEqual(path_row["relative_path"], "app.py")
            self.assertEqual(path_row["repo_relative_path"], "src/app.py")

    def test_sync_derives_deterministic_activity_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            session_path = (
                home / ".claude" / "projects" / "-Users-alice-demo" / "session-1.jsonl"
            )
            write_jsonl(
                session_path,
                [
                    {
                        "timestamp": "2026-04-10T10:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "Ship it"},
                    },
                    {
                        "timestamp": "2026-04-10T10:00:05Z",
                        "type": "assistant",
                        "message": {
                            "id": "msg-1",
                            "model": "claude-3.7",
                            "usage": {"input_tokens": 1, "output_tokens": 2},
                            "content": [
                                {"type": "tool_use", "name": "Edit", "id": "edit-1", "input": {"file_path": "src/app.py"}},
                                {"type": "tool_use", "name": "Bash", "id": "test-1", "input": {"command": "pytest -q"}},
                                {"type": "tool_use", "name": "Bash", "id": "lint-1", "input": {"command": "ruff check src"}},
                                {"type": "tool_use", "name": "Bash", "id": "build-1", "input": {"command": "npm run build"}},
                                {"type": "tool_use", "name": "Bash", "id": "format-1", "input": {"command": "prettier --write src/app.ts"}},
                                {"type": "tool_use", "name": "Bash", "id": "status-1", "input": {"command": "git status --short"}},
                                {"type": "tool_use", "name": "Bash", "id": "diff-1", "input": {"command": "git diff --stat"}},
                                {"type": "tool_use", "name": "Bash", "id": "commit-1", "input": {"command": "git commit -m ship"}},
                            ],
                        },
                    },
                    {
                        "timestamp": "2026-04-10T10:00:06Z",
                        "type": "user",
                        "message": {
                            "content": [
                                {"type": "tool_result", "tool_use_id": "test-1", "is_error": True, "content": "1 failed"},
                                {"type": "tool_result", "tool_use_id": "lint-1", "is_error": True, "content": "E999"},
                                {"type": "tool_result", "tool_use_id": "build-1", "is_error": False, "content": "built"},
                                {"type": "tool_result", "tool_use_id": "format-1", "is_error": False, "content": "formatted"},
                                {"type": "tool_result", "tool_use_id": "status-1", "is_error": False, "content": "M src/app.py"},
                                {"type": "tool_result", "tool_use_id": "diff-1", "is_error": False, "content": " src/app.py | 2 +-"},
                                {"type": "tool_result", "tool_use_id": "commit-1", "is_error": False, "content": "[main abc123] ship"},
                            ]
                        },
                    },
                ],
            )

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
                    SELECT
                        write_path_count,
                        read_path_count,
                        search_path_count,
                        test_run_count,
                        test_failure_count,
                        lint_run_count,
                        lint_failure_count,
                        build_run_count,
                        build_failure_count,
                        format_run_count,
                        format_failure_count,
                        git_status_count,
                        git_diff_count,
                        git_commit_count,
                        activity_version,
                        error_count,
                        session_goal,
                        session_summary,
                        session_outcome,
                        session_status,
                        narrative_version
                    FROM sessions
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(row["write_path_count"], 2)
            self.assertEqual(row["read_path_count"], 0)
            self.assertEqual(row["search_path_count"], 0)
            self.assertEqual(row["test_run_count"], 1)
            self.assertEqual(row["test_failure_count"], 1)
            self.assertEqual(row["lint_run_count"], 1)
            self.assertEqual(row["lint_failure_count"], 1)
            self.assertEqual(row["build_run_count"], 1)
            self.assertEqual(row["build_failure_count"], 0)
            self.assertEqual(row["format_run_count"], 1)
            self.assertEqual(row["format_failure_count"], 0)
            self.assertEqual(row["git_status_count"], 1)
            self.assertEqual(row["git_diff_count"], 1)
            self.assertEqual(row["git_commit_count"], 1)
            self.assertEqual(row["activity_version"], 1)
            self.assertEqual(row["error_count"], 2)
            self.assertEqual(row["session_goal"], "Ship it")
            self.assertIn("Touched 2 files", row["session_summary"])
            self.assertIn("Ran tests 1 time with 1 failure", row["session_summary"])
            self.assertIn("Made 1 git commit", row["session_summary"])
            self.assertEqual(row["session_status"], "partial")
            self.assertIn("left 1 test failure, 1 lint failure", row["session_outcome"])
            self.assertEqual(row["narrative_version"], 1)

    def test_sync_backfills_activity_metrics_for_unchanged_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_claude_session(
                home,
                assistant_content=[
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": "tool-1",
                        "input": {"command": "pytest -q"},
                    }
                ],
            )

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
                    SET activity_version = 0,
                        narrative_version = 0,
                        test_run_count = NULL,
                        test_failure_count = NULL,
                        session_summary = NULL,
                        session_status = NULL
                    WHERE session_id = 'session-1'
                    """
                )
                conn.commit()

            counts = sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            self.assertEqual(counts, (0, 1, 0))

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT activity_version, narrative_version, test_run_count, test_failure_count,
                           session_summary, session_status
                    FROM sessions
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(row["activity_version"], 1)
            self.assertEqual(row["narrative_version"], 1)
            self.assertEqual(row["test_run_count"], 1)
            self.assertEqual(row["test_failure_count"], 0)
            self.assertIn("Ran tests 1 time", row["session_summary"])
            self.assertEqual(row["session_status"], "success")

    def test_sync_backfills_origin_for_unchanged_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_claude_session(
                home,
                message=(
                    "You are a senior statutory-fidelity reviewer for RAC (Rules as Code) encodings.\n\n"
                    "Review the file holistically for citation fidelity."
                ),
            )

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
                    SET origin_version = 0,
                        session_origin = NULL
                    WHERE session_id = 'session-1'
                    """
                )
                conn.commit()

            counts = sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            self.assertEqual(counts, (0, 1, 0))

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT origin_version, session_origin
                    FROM sessions
                    WHERE session_id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(row["origin_version"], 1)
            self.assertEqual(row["session_origin"], "pipeline_eval")

    def test_sync_backfills_missing_session_paths_for_unchanged_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_claude_session(
                home,
                assistant_content=[
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "id": "tool-1",
                        "input": {"file_path": "src/app.py"},
                    }
                ],
            )

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                conn.execute("DELETE FROM session_paths WHERE session_id = 'session-1'")
                conn.execute("UPDATE sessions SET workspace_root = NULL WHERE session_id = 'session-1'")
                conn.commit()

            counts = sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            self.assertEqual(counts, (0, 1, 0))

            with open_sqlite(db_path) as conn:
                session = conn.execute(
                    "SELECT workspace_root FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()
                path_count = conn.execute(
                    "SELECT COUNT(*) FROM session_paths WHERE session_id = 'session-1'"
                ).fetchone()[0]

            self.assertEqual(session["workspace_root"], "/tmp/demo")
            self.assertEqual(path_count, 1)

    def test_session_detail_uses_configured_shared_dir_fallback(self) -> None:
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
                conn.execute(
                    """
                    UPDATE sessions
                    SET shared_path = ?, source_path = ?
                    WHERE session_id = 'session-1'
                    """,
                    ("/other/machine/shared/session-1.jsonl", "/missing/source/session-1.jsonl"),
                )
                conn.commit()

            app = create_app(db_path=db_path, shared_dir=shared)
            with app.test_client() as client:
                response = client.get("/sessions/session-1")

                self.assertEqual(response.status_code, 200)
                self.assertIn(b"hello world", response.data)

    def test_logpile_ignore_file_skips_matching_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_claude_session(home)

            (home / ".logpile-ignore").parent.mkdir(parents=True, exist_ok=True)
            (home / ".logpile-ignore").write_text("*session-1.jsonl\n", encoding="utf-8")

            counts = sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            self.assertEqual(counts, (0, 0, 1))
            self.assertTrue(shared.is_dir())
            self.assertEqual(shared.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(shared.iterdir()), [])

    def test_user_profile_page_renders_from_slug(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"

            self._write_claude_session(home)
            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="Alice Smith",
                machine="machine-1",
                home=home,
            )

            app = create_app(db_path=db_path, shared_dir=shared)
            with app.test_client() as client:
                response = client.get("/u/alice-smith")
                sessions_response = client.get("/api/users/alice-smith/sessions")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(sessions_response.status_code, 200)
                self.assertIn(b"@alice-smith", response.data)
                self.assertIn(b"Recent sessions", response.data)
                self.assertIn(b"alice-smith", sessions_response.data)

    def test_old_db_is_migrated_to_users_and_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "legacy.db"
            with open_sqlite(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE sessions (
                        session_id TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        username TEXT NOT NULL,
                        machine TEXT,
                        project TEXT,
                        source_path TEXT NOT NULL,
                        shared_path TEXT NOT NULL,
                        first_timestamp TEXT,
                        last_timestamp TEXT,
                        duration_seconds REAL,
                        user_message_count INTEGER DEFAULT 0,
                        assistant_message_count INTEGER DEFAULT 0,
                        tool_call_count INTEGER DEFAULT 0,
                        error_count INTEGER DEFAULT 0,
                        total_input_tokens INTEGER DEFAULT 0,
                        total_output_tokens INTEGER DEFAULT 0,
                        first_user_message TEXT,
                        is_private INTEGER DEFAULT 0,
                        file_hash TEXT,
                        synced_at TEXT,
                        model TEXT
                    );
                    """
                )
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, source, username, source_path, shared_path, first_user_message
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-session",
                        "claudecode",
                        "Alice Smith",
                        "/tmp/source.jsonl",
                        "/tmp/shared.jsonl",
                        "hello",
                    ),
                )
                conn.commit()

            init_db(db_path)

            with open_sqlite(db_path) as conn:
                user = conn.execute(
                    "SELECT username, display_name FROM users WHERE username = 'alice-smith'"
                ).fetchone()
                session = conn.execute(
                    "SELECT username, visibility FROM sessions WHERE session_id = 'legacy-session'"
                ).fetchone()
                transition = conn.execute(
                    """
                    SELECT from_visibility, to_visibility, transition_source, warning
                    FROM visibility_transitions
                    WHERE session_id = 'legacy-session'
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()

            self.assertEqual(user[0], "alice-smith")
            self.assertEqual(user[1], "Alice Smith")
            self.assertEqual(session[0], "alice-smith")
            self.assertEqual(session[1], "unlisted")
            self.assertEqual(
                (transition["from_visibility"], transition["to_visibility"]),
                ("private", "unlisted"),
            )
            self.assertEqual(transition["transition_source"], "migration")
            self.assertIn("local/link artifacts", transition["warning"])

    def test_user_default_visibility_applies_to_new_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            init_db(db_path)
            with open_sqlite(db_path) as conn:
                ensure_user(conn, "alice", display_name="Alice")
                update_user(conn, "alice", default_session_visibility="public")
                conn.commit()

            self._write_claude_session(home, session_id="session-1", message="first")
            with self.assertWarnsRegex(RuntimeWarning, "kept this session unlisted"):
                sync_sessions(
                    shared_dir=shared,
                    db_path=db_path,
                    username="alice",
                    machine="machine-1",
                    home=home,
                )

            with open_sqlite(db_path) as conn:
                update_user(conn, "alice", default_session_visibility="unlisted")
                conn.commit()

            self._write_claude_session(home, session_id="session-2", message="second")
            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                rows = conn.execute(
                    "SELECT session_id, visibility FROM sessions ORDER BY session_id"
                ).fetchall()
                guarded = conn.execute(
                    """
                    SELECT to_visibility, warning
                    FROM visibility_transitions
                    WHERE session_id = 'session-1'
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()

            self.assertEqual((rows[0]["session_id"], rows[0]["visibility"]), ("session-1", "unlisted"))
            self.assertEqual((rows[1]["session_id"], rows[1]["visibility"]), ("session-2", "unlisted"))
            self.assertEqual(guarded["to_visibility"], "unlisted")
            self.assertIn("successful review record", guarded["warning"])


# --- archive-integrity tests -------------------------------------------------
# Imports are grouped here (not at top of file) so this block stays a single
# self-contained appended hunk.
import errno as _errno
import fcntl as _fcntl
import shutil as _shutil
from unittest import mock as _mock

from logpile import sync as _sync_module
from logpile.sync import _copy_session


class CopySessionAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.src = root / "src" / "session.jsonl"
        self.src.parent.mkdir(parents=True)
        self.src.write_text('{"a": 1}\n')
        self.dst = root / "shared" / "session.jsonl"
        self.dst.parent.mkdir(parents=True)

    def _tmp_leftovers(self) -> list[Path]:
        return list(self.dst.parent.glob("*.tmp-sync"))

    def test_copies_new_file_and_leaves_no_temp(self) -> None:
        self.assertTrue(_copy_session(self.src, self.dst))
        self.assertEqual(self.dst.read_text(), self.src.read_text())
        self.assertEqual(self.dst.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self._tmp_leftovers(), [])

    def test_staging_file_is_0600_under_umask_022(self) -> None:
        observed_modes: list[int] = []
        original = _sync_module.shutil.copyfileobj

        def inspect_mode(source, target):
            observed_modes.append(os.fstat(target.fileno()).st_mode & 0o777)
            return original(source, target)

        old_umask = os.umask(0o022)
        try:
            with _mock.patch.object(
                _sync_module.shutil, "copyfileobj", side_effect=inspect_mode
            ):
                _copy_session(self.src, self.dst)
        finally:
            os.umask(old_umask)

        self.assertEqual(observed_modes, [0o600])

    def test_skips_identical_existing_copy(self) -> None:
        _shutil.copy2(self.src, self.dst)
        self.dst.parent.chmod(0o755)
        self.dst.chmod(0o644)
        old_umask = os.umask(0o022)
        try:
            self.assertFalse(_copy_session(self.src, self.dst))
        finally:
            os.umask(old_umask)
        self.assertEqual(self.dst.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.dst.stat().st_mode & 0o777, 0o600)

    def test_replaces_stale_copy(self) -> None:
        self.dst.write_text("stale\n")
        self.assertTrue(_copy_session(self.src, self.dst))
        self.assertEqual(self.dst.read_text(), self.src.read_text())
        self.assertEqual(self._tmp_leftovers(), [])

    def test_upgrades_enospc_symlink_to_real_copy(self) -> None:
        self.dst.symlink_to(self.src)
        self.assertTrue(_copy_session(self.src, self.dst))
        self.assertFalse(self.dst.is_symlink())
        self.assertEqual(self.dst.read_text(), self.src.read_text())

    def test_generic_error_preserves_existing_copy_and_cleans_temp(self) -> None:
        self.dst.write_text("previous complete copy\n")

        def boom(src, dst):
            dst.write(b"partial")
            raise OSError(_errno.EIO, "boom")

        with (
            _mock.patch.object(_sync_module.shutil, "copyfileobj", side_effect=boom),
            self.assertRaises(OSError),
        ):
            _copy_session(self.src, self.dst)
        self.assertEqual(self.dst.read_text(), "previous complete copy\n")
        self.assertEqual(self._tmp_leftovers(), [])

    def test_enospc_fails_closed_and_keeps_existing_complete_copy(self) -> None:
        self.dst.write_text("previous complete copy\n")

        def full(src, dst):
            raise OSError(_errno.ENOSPC, "disk full")

        with (
            _mock.patch.object(_sync_module.shutil, "copyfileobj", side_effect=full),
            self.assertRaises(OSError) as raised,
        ):
            _copy_session(self.src, self.dst)
        self.assertEqual(raised.exception.errno, _errno.ENOSPC)
        self.assertFalse(self.dst.is_symlink())
        self.assertEqual(self.dst.read_text(), "previous complete copy\n")

    def test_enospc_without_existing_copy_never_creates_symlink(self) -> None:
        def full(src, dst):
            raise OSError(_errno.ENOSPC, "disk full")

        with (
            _mock.patch.object(_sync_module.shutil, "copyfileobj", side_effect=full),
            self.assertRaises(OSError) as raised,
        ):
            _copy_session(self.src, self.dst)
        self.assertEqual(raised.exception.errno, _errno.ENOSPC)
        self.assertFalse(self.dst.exists())
        self.assertFalse(self.dst.is_symlink())
        self.assertEqual(self._tmp_leftovers(), [])


class SyncCoverageAndFastPathTests(unittest.TestCase):
    """Multi-root codex discovery, per-day usage rows, and the size+mtime
    fast path that keeps 20GB+ archives from being re-hashed every sync."""

    # Reuse the session-writing fixtures without re-running SyncTests' cases.
    _write_claude_session = SyncTests._write_claude_session
    _write_codex_session = SyncTests._write_codex_session

    @staticmethod
    def _copy_fixture(relative_path: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(FIXTURES / relative_path, destination)
        return destination

    def _write_codex_rollout(
        self,
        home: Path,
        *,
        root: str,
        session_id: str,
        message: str = "Fix the parser",
    ) -> Path:
        session_path = home / root / f"{session_id}.jsonl"
        write_jsonl(
            session_path,
            [
                {
                    "timestamp": "2026-04-10T10:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "timestamp": "2026-04-10T10:00:00Z",
                        "cwd": "/tmp/demo",
                    },
                },
                {
                    "timestamp": "2026-04-10T10:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": message}],
                    },
                },
                {
                    "timestamp": "2026-04-10T10:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 1200,
                                "cached_input_tokens": 300,
                                "output_tokens": 45,
                                "total_tokens": 1245,
                            }
                        },
                    },
                },
            ],
        )
        return session_path

    def test_fixture_lineage_resolves_to_exact_rollout_keys_child_first(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            child_name = "rollout-2026-06-08T11-39-04-leaf-thread.jsonl"
            parent_name = "rollout-2026-05-01T00-00-00-parent-thread.jsonl"

            # Directory order deliberately presents the child first. Parent
            # resolution must happen after the complete scan, not per file.
            child = self._copy_fixture(
                f"codex/{child_name}",
                home / ".codex" / "sessions" / "2026" / "01" / "01" / child_name,
            )
            self._copy_fixture(
                f"codex/{parent_name}",
                home / ".codex" / "sessions" / "2026" / "12" / "31" / parent_name,
            )

            result = sync_sessions(shared, db_path, "alice", "m1", home)
            self.assertEqual(result.new, 2)

            child_id = Path(child_name).stem
            parent_id = Path(parent_name).stem
            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT thread_id, parent_thread_id, parent_session_id,
                           spawn_depth, first_timestamp, identity_version
                    FROM sessions WHERE session_id = ?
                    """,
                    (child_id,),
                ).fetchone()
                orphan_count = conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM sessions AS child
                    LEFT JOIN sessions AS parent
                      ON parent.session_id = child.parent_session_id
                     AND parent.username = child.username
                     AND parent.source = child.source
                    WHERE child.parent_session_id IS NOT NULL
                      AND parent.session_id IS NULL
                    """
                ).fetchone()["n"]

            self.assertEqual(row["thread_id"], "leaf-thread")
            self.assertEqual(row["parent_thread_id"], "parent-thread")
            self.assertEqual(row["parent_session_id"], parent_id)
            self.assertEqual(row["spawn_depth"], 1)
            self.assertEqual(row["first_timestamp"], "2026-06-08T11:39:04.000Z")
            self.assertEqual(row["identity_version"], SESSION_IDENTITY_VERSION)
            self.assertEqual(orphan_count, 0)

            # Rotate the child away and simulate every parser-derived field
            # left stale by the old replay heuristic. The durable shared copy
            # must restore live messages/tools/usage as well as lineage.
            child.unlink()
            with open_sqlite(db_path) as conn:
                conn.execute(
                    """
                    UPDATE sessions
                    SET token_version = 0,
                        identity_version = 0,
                        thread_id = NULL,
                        parent_thread_id = NULL,
                        parent_session_id = 'parent-thread',
                        spawn_depth = 0,
                        user_message_count = 0,
                        assistant_message_count = 0,
                        tool_call_count = 0,
                        total_input_tokens = 0,
                        total_output_tokens = 0
                    WHERE session_id = ?
                    """,
                    (child_id,),
                )
                conn.execute("DELETE FROM tool_calls WHERE session_id = ?", (child_id,))
                conn.execute(
                    "DELETE FROM session_daily_usage WHERE session_id = ?", (child_id,)
                )
                conn.commit()

            result = sync_sessions(shared, db_path, "alice", "m1", home)
            self.assertGreaterEqual(result.updated, 1)
            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT thread_id, parent_thread_id, parent_session_id,
                           spawn_depth, user_message_count, tool_call_count,
                           total_input_tokens, total_output_tokens,
                           token_version, identity_version
                    FROM sessions WHERE session_id = ?
                    """,
                    (child_id,),
                ).fetchone()
                tools = conn.execute(
                    "SELECT command FROM tool_calls WHERE session_id = ?", (child_id,)
                ).fetchall()
                daily = conn.execute(
                    """
                    SELECT COALESCE(SUM(total_input_tokens), 0) AS input_tokens,
                           COALESCE(SUM(total_output_tokens), 0) AS output_tokens
                    FROM session_daily_usage WHERE session_id = ?
                    """,
                    (child_id,),
                ).fetchone()

            self.assertEqual(row["thread_id"], "leaf-thread")
            self.assertEqual(row["parent_thread_id"], "parent-thread")
            self.assertEqual(row["parent_session_id"], parent_id)
            self.assertEqual(row["spawn_depth"], 1)
            self.assertEqual(row["user_message_count"], 1)
            self.assertEqual(row["tool_call_count"], 1)
            self.assertEqual([tool["command"] for tool in tools], ["ruff check ."])
            self.assertEqual(daily["input_tokens"], row["total_input_tokens"])
            self.assertEqual(daily["output_tokens"], row["total_output_tokens"])
            self.assertEqual(row["token_version"], SESSION_TOKEN_VERSION)
            self.assertEqual(row["identity_version"], SESSION_IDENTITY_VERSION)

            # Canonical keys fail closed while raw evidence remains available:
            # neither an unresolved thread nor a self-thread may become an
            # orphan/self edge in the stored session graph.
            with open_sqlite(db_path) as conn:
                conn.execute(
                    """
                    UPDATE sessions
                    SET parent_thread_id = 'missing-thread',
                        parent_session_id = session_id
                    WHERE session_id = ?
                    """,
                    (child_id,),
                )
                conn.execute(
                    """
                    UPDATE sessions
                    SET parent_thread_id = thread_id,
                        parent_session_id = session_id
                    WHERE session_id = ?
                    """,
                    (parent_id,),
                )
                conn.commit()

            sync_sessions(shared, db_path, "alice", "m1", home)
            with open_sqlite(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT session_id, parent_thread_id, parent_session_id
                    FROM sessions WHERE session_id IN (?, ?)
                    ORDER BY session_id
                    """,
                    (child_id, parent_id),
                ).fetchall()
            self.assertTrue(all(row["parent_session_id"] is None for row in rows))
            self.assertEqual(
                {row["parent_thread_id"] for row in rows},
                {"missing-thread", "parent-thread"},
            )

    def test_fixture_claude_subagent_identity_and_rotated_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            project_root = home / ".claude" / "projects" / "-tmp-demo"
            root_session = self._copy_fixture(
                "claudecode/-tmp-demo/root-session.jsonl",
                project_root / "root-session.jsonl",
            )
            child = self._copy_fixture(
                "claudecode/-tmp-demo/root-session/subagents/agent-worker.jsonl",
                project_root / "root-session" / "subagents" / "agent-worker.jsonl",
            )
            shared = root / "shared"
            db_path = root / "logpile.db"

            result = sync_sessions(shared, db_path, "alice", "m1", home)
            self.assertEqual(result.new, 2)
            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT project, parent_session_id, spawn_depth,
                           identity_version, native_total_output_tokens
                    FROM sessions WHERE session_id = 'agent-worker'
                    """
                ).fetchone()
            self.assertEqual(row["project"], "demo")
            self.assertEqual(row["parent_session_id"], "root-session")
            self.assertGreaterEqual(row["spawn_depth"], 1)
            self.assertEqual(row["identity_version"], SESSION_IDENTITY_VERSION)
            self.assertEqual(row["native_total_output_tokens"], 40)

            child.unlink()
            self.assertTrue(root_session.exists())
            with open_sqlite(db_path) as conn:
                conn.execute(
                    """
                    UPDATE sessions
                    SET token_version = 0,
                        identity_version = 0,
                        parent_session_id = NULL,
                        spawn_depth = 0,
                        user_message_count = 0,
                        assistant_message_count = 0,
                        total_output_tokens = 0,
                        native_total_output_tokens = 0
                    WHERE session_id = 'agent-worker'
                    """
                )
                conn.execute(
                    "DELETE FROM session_daily_usage WHERE session_id = 'agent-worker'"
                )
                conn.commit()

            result = sync_sessions(shared, db_path, "alice", "m1", home)
            self.assertGreaterEqual(result.updated, 1)
            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT project, parent_session_id, spawn_depth,
                           user_message_count, assistant_message_count,
                           total_output_tokens, native_total_output_tokens,
                           token_version, identity_version
                    FROM sessions WHERE session_id = 'agent-worker'
                    """
                ).fetchone()
                orphan_count = conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM sessions AS child
                    LEFT JOIN sessions AS parent
                      ON parent.session_id = child.parent_session_id
                     AND parent.username = child.username
                     AND parent.source = child.source
                    WHERE child.parent_session_id IS NOT NULL
                      AND parent.session_id IS NULL
                    """
                ).fetchone()["n"]

            self.assertEqual(row["project"], "demo")
            self.assertEqual(row["parent_session_id"], "root-session")
            self.assertGreaterEqual(row["spawn_depth"], 1)
            self.assertEqual(row["user_message_count"], 1)
            self.assertEqual(row["assistant_message_count"], 1)
            self.assertEqual(row["total_output_tokens"], 40)
            self.assertEqual(row["native_total_output_tokens"], 40)
            self.assertEqual(row["token_version"], SESSION_TOKEN_VERSION)
            self.assertEqual(row["identity_version"], SESSION_IDENTITY_VERSION)
            self.assertEqual(orphan_count, 0)

    def test_workflow_journal_cannot_overwrite_full_agent_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            project_root = home / ".claude" / "projects" / "-tmp-demo"
            self._copy_fixture(
                "claudecode/-tmp-demo/root-session.jsonl",
                project_root / "root-session.jsonl",
            )
            self._copy_fixture(
                "claudecode/-tmp-demo/root-session/subagents/agent-worker.jsonl",
                project_root / "root-session" / "subagents" / "agent-worker.jsonl",
            )
            journal = self._copy_fixture(
                (
                    "claudecode/-tmp-demo/root-session/subagents/workflows/"
                    "wf-fixture/journal.jsonl"
                ),
                (
                    project_root
                    / "root-session"
                    / "subagents"
                    / "workflows"
                    / "wf-fixture"
                    / "journal.jsonl"
                ),
            )
            shared = root / "shared"
            db_path = root / "logpile.db"

            first = sync_sessions(shared, db_path, "alice", "m1", home)
            self.assertEqual(first.new, 2)
            with open_sqlite(db_path) as conn:
                agent = conn.execute(
                    "SELECT total_output_tokens, source_path FROM sessions "
                    "WHERE session_id = 'agent-worker'"
                ).fetchone()
                self.assertEqual(agent["total_output_tokens"], 40)
                self.assertTrue(agent["source_path"].endswith("agent-worker.jsonl"))
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM sessions WHERE session_id = 'journal'"
                    ).fetchone()
                )

                # Simulate the one stem-keyed zero-usage row written by older
                # syncs. The exact journal path is enough to retire it safely.
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, source, username, source_path, shared_path,
                        token_version, identity_version
                    ) VALUES ('journal', 'claudecode', 'alice', ?, '', 0, 0)
                    """,
                    (str(journal),),
                )
                conn.commit()

            second = sync_sessions(shared, db_path, "alice", "m1", home)
            self.assertEqual(second.new, 0)
            self.assertGreaterEqual(second.updated, 1)
            with open_sqlite(db_path) as conn:
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM sessions WHERE session_id = 'journal'"
                    ).fetchone()
                )
                agent = conn.execute(
                    "SELECT total_output_tokens, source_path FROM sessions "
                    "WHERE session_id = 'agent-worker'"
                ).fetchone()
                self.assertEqual(agent["total_output_tokens"], 40)
                self.assertTrue(agent["source_path"].endswith("agent-worker.jsonl"))

    def test_fixture_unknown_cache_and_approximated_daily_fields_persist(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            project_root = home / ".claude" / "projects" / "-tmp-demo"
            self._copy_fixture(
                "claudecode/cache-unknown-remainder.jsonl",
                project_root / "cache-unknown-remainder.jsonl",
            )
            self._copy_fixture(
                "claudecode/residual-day.jsonl",
                project_root / "residual-day.jsonl",
            )

            result = sync_sessions(
                root / "shared", root / "logpile.db", "alice", "m1", home
            )
            self.assertEqual(result.new, 2)
            with open_sqlite(root / "logpile.db") as conn:
                cache_session = conn.execute(
                    """
                    SELECT cache_creation_input_tokens,
                           cache_creation_5m_input_tokens,
                           cache_creation_1h_input_tokens,
                           cache_creation_unknown_input_tokens
                    FROM sessions
                    WHERE session_id = 'cache-unknown-remainder'
                    """
                ).fetchone()
                cache_day = conn.execute(
                    """
                    SELECT cache_creation_input_tokens,
                           cache_creation_5m_input_tokens,
                           cache_creation_1h_input_tokens,
                           cache_creation_unknown_input_tokens
                    FROM session_daily_usage
                    WHERE session_id = 'cache-unknown-remainder'
                    """
                ).fetchone()
                residual_session = conn.execute(
                    """
                    SELECT total_input_tokens, total_output_tokens,
                           user_message_count, assistant_message_count
                    FROM sessions WHERE session_id = 'residual-day'
                    """
                ).fetchone()
                residual_day = conn.execute(
                    """
                    SELECT total_input_tokens, total_output_tokens,
                           user_message_count, assistant_message_count,
                           approximated
                    FROM session_daily_usage
                    WHERE session_id = 'residual-day'
                    """
                ).fetchone()

            self.assertEqual(cache_session["cache_creation_input_tokens"], 600)
            self.assertEqual(cache_session["cache_creation_5m_input_tokens"], 0)
            self.assertEqual(cache_session["cache_creation_1h_input_tokens"], 0)
            self.assertEqual(cache_session["cache_creation_unknown_input_tokens"], 600)
            self.assertEqual(dict(cache_day), dict(cache_session))
            self.assertEqual(residual_day["approximated"], 1)
            for field in (
                "total_input_tokens",
                "total_output_tokens",
                "user_message_count",
                "assistant_message_count",
            ):
                self.assertEqual(residual_day[field], residual_session[field])

    def test_sync_scans_archived_and_extra_codex_homes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            archived = self._write_codex_rollout(
                home, root=".codex/archived_sessions/2026/03/17", session_id="rollout-archived"
            )
            extra_home = self._write_codex_rollout(
                home, root=".codex-2/sessions/2026/04/10", session_id="rollout-codex2"
            )
            openclaw = self._write_codex_rollout(
                home,
                root=".openclaw/agents/bot/agent/codex-home/sessions/2026/04",
                session_id="rollout-openclaw",
            )

            sync_sessions(root / "shared", root / "logpile.db", "alice", "m1", home)

            with open_sqlite(root / "logpile.db") as conn:
                rows = {
                    row["session_id"]: row["source_path"]
                    for row in conn.execute(
                        "SELECT session_id, source_path FROM sessions WHERE source = 'codex'"
                    )
                }
            self.assertEqual(rows["rollout-archived"], str(archived))
            self.assertEqual(rows["rollout-codex2"], str(extra_home))
            self.assertEqual(rows["rollout-openclaw"], str(openclaw))

    def test_sync_prefers_live_copy_when_stem_exists_in_archive_too(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            live = self._write_codex_rollout(
                home, root=".codex/sessions/2026/04/10", session_id="rollout-dup",
                message="live continuation",
            )
            self._write_codex_rollout(
                home, root=".codex/archived_sessions/2026/04/10", session_id="rollout-dup",
                message="stale archived copy",
            )

            sync_sessions(root / "shared", root / "logpile.db", "alice", "m1", home)

            with open_sqlite(root / "logpile.db") as conn:
                row = conn.execute(
                    "SELECT source_path, first_user_message FROM sessions WHERE session_id = 'rollout-dup'"
                ).fetchone()
            self.assertEqual(row["source_path"], str(live))
            self.assertEqual(row["first_user_message"], "live continuation")

    def test_sync_writes_daily_usage_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            self._write_claude_session(home, session_id="claude-daily")
            self._write_codex_session(home, session_id="rollout-daily")

            sync_sessions(root / "shared", root / "logpile.db", "alice", "m1", home)

            with open_sqlite(root / "logpile.db") as conn:
                claude_day = conn.execute(
                    "SELECT * FROM session_daily_usage WHERE session_id = 'claude-daily'"
                ).fetchone()
                codex_day = conn.execute(
                    "SELECT * FROM session_daily_usage WHERE session_id = 'rollout-daily'"
                ).fetchone()
                effective = conn.execute(
                    """
                    SELECT day, SUM(total_output_tokens) AS out_tokens, MIN(approximated) AS approximated
                    FROM session_daily_effective
                    GROUP BY day
                    """
                ).fetchone()

            self.assertEqual(claude_day["day"], "2026-04-10")
            self.assertEqual(claude_day["total_input_tokens"], 1)
            self.assertEqual(claude_day["total_output_tokens"], 2)
            self.assertEqual(claude_day["user_message_count"], 1)
            self.assertEqual(claude_day["assistant_message_count"], 1)
            self.assertEqual(codex_day["day"], "2026-04-10")
            self.assertEqual(codex_day["total_input_tokens"], 1200)
            self.assertEqual(codex_day["cached_input_tokens"], 300)
            self.assertEqual(codex_day["total_output_tokens"], 45)
            self.assertEqual(effective["day"], "2026-04-10")
            self.assertEqual(effective["out_tokens"], 47)
            self.assertEqual(effective["approximated"], 0)

    def test_token_version_migrates_existing_daily_buckets_to_utc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            session_path = (
                home
                / ".claude"
                / "projects"
                / "-Users-alice-demo"
                / "utc-migration.jsonl"
            )
            write_jsonl(
                session_path,
                [
                    {
                        "timestamp": "2026-04-10T23:30:00-02:00",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "cross midnight in UTC"},
                    },
                    {
                        "timestamp": "2026-04-10T23:30:05-02:00",
                        "type": "assistant",
                        "message": {
                            "id": "utc-msg",
                            "model": "claude-3.7",
                            "usage": {"input_tokens": 1, "output_tokens": 2},
                            "content": [{"type": "text", "text": "done"}],
                        },
                    },
                ],
            )
            shared = root / "shared"
            db_path = root / "logpile.db"
            sync_sessions(shared, db_path, "alice", "m1", home)

            self.assertEqual(SESSION_TOKEN_VERSION, 10)
            with open_sqlite(db_path) as conn:
                conn.execute(
                    "UPDATE sessions SET token_version = 5 "
                    "WHERE session_id = 'utc-migration'"
                )
                conn.execute(
                    "UPDATE session_daily_usage SET day = '2026-04-10' "
                    "WHERE session_id = 'utc-migration'"
                )
                conn.execute(
                    "UPDATE message_claims SET day = '2026-04-10' "
                    "WHERE session_id = 'utc-migration'"
                )
                conn.commit()

            result = sync_sessions(shared, db_path, "alice", "m1", home)
            self.assertEqual((result.new, result.updated), (0, 1))
            with open_sqlite(db_path) as conn:
                session = conn.execute(
                    "SELECT token_version FROM sessions "
                    "WHERE session_id = 'utc-migration'"
                ).fetchone()
                daily_days = [
                    row["day"]
                    for row in conn.execute(
                        "SELECT day FROM session_daily_usage "
                        "WHERE session_id = 'utc-migration' ORDER BY day"
                    )
                ]
                claim_days = [
                    row["day"]
                    for row in conn.execute(
                        "SELECT day FROM message_claims "
                        "WHERE session_id = 'utc-migration' ORDER BY day"
                    )
                ]

            self.assertEqual(session["token_version"], SESSION_TOKEN_VERSION)
            self.assertEqual(daily_days, ["2026-04-11"])
            self.assertEqual(claim_days, ["2026-04-11"])

    def test_sync_persists_cache_creation_on_session_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            write_jsonl(
                home / ".claude" / "projects" / "-Users-alice-demo" / "cache-writes.jsonl",
                [
                    {
                        "timestamp": "2026-07-02T10:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "hello"},
                    },
                    {
                        "timestamp": "2026-07-02T10:00:05Z",
                        "type": "assistant",
                        "message": {
                            "id": "msg-1",
                            "model": "claude-fable-5",
                            "usage": {
                                "input_tokens": 10,
                                "cache_creation_input_tokens": 9_000,
                                "cache_creation": {
                                    "ephemeral_5m_input_tokens": 1_000,
                                    "ephemeral_1h_input_tokens": 8_000,
                                },
                                "cache_read_input_tokens": 20_000,
                                "output_tokens": 50,
                            },
                            "content": [{"type": "text", "text": "hi"}],
                        },
                    },
                ],
            )

            sync_sessions(root / "shared", root / "logpile.db", "alice", "m1", home)

            with open_sqlite(root / "logpile.db") as conn:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE session_id = 'cache-writes'"
                ).fetchone()
            self.assertEqual(row["cache_creation_input_tokens"], 9_000)
            self.assertEqual(row["cache_creation_5m_input_tokens"], 1_000)
            self.assertEqual(row["cache_creation_1h_input_tokens"], 8_000)
            self.assertEqual(row["total_input_tokens"], 10 + 9_000 + 20_000)

    def test_second_sync_skips_without_rehashing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            self._write_claude_session(home, session_id="claude-fast")
            self._write_codex_session(home, session_id="rollout-fast")
            shared = root / "shared"
            db_path = root / "logpile.db"

            sync_sessions(shared, db_path, "alice", "m1", home)

            with mock.patch(
                "logpile.sync.file_hash",
                side_effect=AssertionError("file_hash called on unchanged files"),
            ):
                new, updated, skipped = sync_sessions(shared, db_path, "alice", "m1", home)
            self.assertEqual((new, updated), (0, 0))
            self.assertEqual(skipped, 2)

    def test_rotated_source_backfills_tokens_from_shared_copy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            live = self._write_codex_session(home, session_id="rollout-rotated")
            shared = root / "shared"
            db_path = root / "logpile.db"

            sync_sessions(shared, db_path, "alice", "m1", home)

            # Transcript rotates away; only the shared copy survives. A later
            # accounting fix (token_version bump, simulated by resetting) must
            # still reach this session.
            live.unlink()
            with open_sqlite(db_path) as conn:
                conn.execute(
                    "UPDATE sessions SET token_version = 0, total_input_tokens = 0 "
                    "WHERE session_id = 'rollout-rotated'"
                )
                conn.execute(
                    "DELETE FROM session_daily_usage WHERE session_id = 'rollout-rotated'"
                )
                conn.commit()

            _, updated, _ = sync_sessions(shared, db_path, "alice", "m1", home)
            self.assertGreaterEqual(updated, 1)

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT total_input_tokens, cached_input_tokens, token_version "
                    "FROM sessions WHERE session_id = 'rollout-rotated'"
                ).fetchone()
                daily = conn.execute(
                    "SELECT COUNT(*) AS n FROM session_daily_usage "
                    "WHERE session_id = 'rollout-rotated'"
                ).fetchone()
            self.assertEqual(row["total_input_tokens"], 1200)
            self.assertEqual(row["cached_input_tokens"], 300)
            self.assertEqual(row["token_version"], SESSION_TOKEN_VERSION)
            self.assertEqual(daily["n"], 1)

    def test_moved_rollout_updates_source_path_without_reparse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            live = self._write_codex_session(home, session_id="rollout-move")
            shared = root / "shared"
            db_path = root / "logpile.db"

            sync_sessions(shared, db_path, "alice", "m1", home)

            archived = home / ".codex" / "archived_sessions" / live.name
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(live), str(archived))

            sync_sessions(shared, db_path, "alice", "m1", home)

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT source_path, total_input_tokens FROM sessions WHERE session_id = 'rollout-move'"
                ).fetchone()
            self.assertEqual(row["source_path"], str(archived))
            self.assertEqual(row["total_input_tokens"], 1200)

    def test_live_to_archive_rename_between_stat_and_hash_is_retried_in_archive_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            live = self._write_codex_session(home, session_id="rollout-race")
            archived = home / ".codex" / "archived_sessions" / live.name
            original_hash = _sync_module.file_hash
            rotated = False

            def rotate_before_hash(path: Path) -> str:
                nonlocal rotated
                if path == live and not rotated:
                    rotated = True
                    archived.parent.mkdir(parents=True, exist_ok=True)
                    live.replace(archived)
                return original_hash(path)

            with mock.patch("logpile.sync.file_hash", side_effect=rotate_before_hash):
                result = sync_sessions(
                    root / "shared", root / "logpile.db", "alice", "m1", home
                )

            self.assertEqual(result.new, 1)
            with open_sqlite(root / "logpile.db") as conn:
                row = conn.execute(
                    "SELECT source_path FROM sessions WHERE session_id = 'rollout-race'"
                ).fetchone()
            self.assertEqual(row["source_path"], str(archived))

    def test_live_to_archive_rename_between_hash_and_parse_is_retried_in_archive_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            live = self._write_codex_session(home, session_id="rollout-parse-race")
            archived = home / ".codex" / "archived_sessions" / live.name
            original_parse = _sync_module.parse_codex_session
            rotated = False

            def rotate_before_parse(path: Path):
                nonlocal rotated
                if path == live and not rotated:
                    rotated = True
                    archived.parent.mkdir(parents=True, exist_ok=True)
                    live.replace(archived)
                return original_parse(path)

            with mock.patch(
                "logpile.sync.parse_codex_session", side_effect=rotate_before_parse
            ):
                result = sync_sessions(
                    root / "shared", root / "logpile.db", "alice", "m1", home
                )

            self.assertEqual(result.new, 1)
            with open_sqlite(root / "logpile.db") as conn:
                row = conn.execute(
                    "SELECT source_path FROM sessions WHERE session_id = 'rollout-parse-race'"
                ).fetchone()
            self.assertEqual(row["source_path"], str(archived))


class SyncLockTests(unittest.TestCase):
    def test_concurrent_sync_skips_instead_of_interleaving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            db_path = root / "logpile.db"
            shared = root / "shared"
            lock_path = Path(f"{db_path}.sync.lock")
            with open(lock_path, "w") as holder:
                _fcntl.flock(holder, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                result = sync_sessions(shared, db_path, "alice", "test-machine", home)
            self.assertEqual(result, (0, 0, 0))
            self.assertIsInstance(result, _sync_module.SyncLockContended)
            self.assertEqual(result.status, _sync_module.SyncStatus.LOCK_CONTENDED)
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
            # Skipped before init_db: no database was created.
            self.assertFalse(db_path.exists())

    def test_eacces_from_flock_is_typed_contention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            with _mock.patch.object(
                _sync_module.fcntl,
                "flock",
                side_effect=OSError(_errno.EACCES, "contended"),
            ):
                result = sync_sessions(
                    root / "shared", root / "logpile.db", "alice", "m1", home
                )
            self.assertIsInstance(result, _sync_module.SyncLockContended)

    def test_unsupported_flock_error_is_not_reported_as_contention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            with _mock.patch.object(
                _sync_module.fcntl,
                "flock",
                side_effect=OSError(_errno.ENOTSUP, "locking unsupported"),
            ), self.assertRaises(_sync_module.SyncLockError):
                sync_sessions(
                    root / "shared", root / "logpile.db", "alice", "m1", home
                )

    def test_sync_lock_rejects_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            db_path = root / "logpile.db"
            lock_path = Path(f"{db_path}.sync.lock")
            target = root / "unmanaged-target"
            target.write_text("leave me alone\n", encoding="utf-8")
            target.chmod(0o644)
            lock_path.symlink_to(target)

            with self.assertRaisesRegex(
                _sync_module.SyncLockError, "safely open sync lock"
            ):
                sync_sessions(root / "shared", db_path, "alice", "m1", home)

            self.assertEqual(target.read_text(encoding="utf-8"), "leave me alone\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            self.assertFalse(db_path.exists())

    def test_sync_lock_rejects_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            db_path = root / "logpile.db"
            lock_path = Path(f"{db_path}.sync.lock")
            os.mkfifo(lock_path)

            with self.assertRaisesRegex(
                _sync_module.SyncLockError, "safely open sync lock"
            ):
                sync_sessions(root / "shared", db_path, "alice", "m1", home)

            self.assertFalse(db_path.exists())

    def test_sync_lock_fstat_rejects_nonregular_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            fake_stat = _mock.Mock(st_mode=0o010600)
            with _mock.patch.object(
                _sync_module.os, "fstat", return_value=fake_stat
            ), self.assertRaisesRegex(
                _sync_module.SyncLockError, "non-regular sync lock"
            ):
                sync_sessions(
                    root / "shared",
                    root / "logpile.db",
                    "alice",
                    "m1",
                    home,
                )


class RuntimePermissionTests(unittest.TestCase):
    _write_claude_session = SyncTests._write_claude_session

    def test_runtime_storage_is_private_under_umask_022(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            home = root / "home"
            source = self._write_claude_session(home)
            db_path = runtime / "logpile.db"
            shared = runtime / "shared"

            old_umask = os.umask(0o022)
            try:
                sync_sessions(shared, db_path, "alice", "m1", home)
                with _sync_module.get_db(db_path) as conn:
                    conn.execute(
                        "UPDATE users SET updated_at = updated_at WHERE username = 'alice'"
                    )
                    mode_paths = [
                        db_path,
                        Path(f"{db_path}-wal"),
                        Path(f"{db_path}-shm"),
                    ]
                    for path in mode_paths:
                        self.assertTrue(path.exists(), path)
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
            finally:
                os.umask(old_umask)

            copied = shared / "alice" / "claudecode" / "demo" / source.name
            lock_path = Path(f"{db_path}.sync.lock")
            self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
            self.assertEqual(shared.stat().st_mode & 0o777, 0o700)
            for directory in (copied.parent, copied.parent.parent, copied.parent.parent.parent):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700, directory)
            self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)

    def test_unchanged_sync_upgrades_existing_managed_storage_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            home = root / "home"
            source = self._write_claude_session(home)
            db_path = runtime / "logpile.db"
            shared = runtime / "shared"
            sync_sessions(shared, db_path, "alice", "m1", home)

            copied = shared / "alice" / "claudecode" / "demo" / source.name
            lock_path = Path(f"{db_path}.sync.lock")
            managed_directories = [
                runtime,
                shared,
                shared / "alice",
                shared / "alice" / "claudecode",
                copied.parent,
            ]
            for directory in managed_directories:
                directory.chmod(0o755)
            for artifact in (db_path, lock_path, copied):
                artifact.chmod(0o644)

            old_umask = os.umask(0o022)
            try:
                with _mock.patch.object(
                    _sync_module,
                    "file_hash",
                    side_effect=AssertionError(
                        "unchanged permission upgrade must retain the no-hash fast path"
                    ),
                ):
                    result = sync_sessions(shared, db_path, "alice", "m1", home)
            finally:
                os.umask(old_umask)

            self.assertEqual(result, (0, 0, 1))
            for directory in managed_directories:
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700, directory)
            for artifact in (db_path, lock_path, copied):
                self.assertEqual(artifact.stat().st_mode & 0o777, 0o600, artifact)


if __name__ == "__main__":
    unittest.main()
