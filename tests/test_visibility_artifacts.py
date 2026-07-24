import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from logpile.cli import cli
from logpile.db import ensure_user, init_db, update_user
from logpile.parsers import render_claudecode_transcript
from logpile.publish import open_verified_public_artifact
from logpile.sync import StorageSafetyError, sync_sessions
from logpile.web.app import create_app


def _write_session(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-07-11T12:00:00Z",
            "type": "user",
            "cwd": "/tmp/demo",
            "message": {"content": body},
        },
        {
            "timestamp": "2026-07-11T12:00:01Z",
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "model": "claude-test",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"type": "text", "text": "done"}],
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _connect(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


class VisibilityArtifactTests(unittest.TestCase):
    def _paths(self, root: Path) -> tuple[Path, Path, Path, Path]:
        home = root / "home"
        shared = root / "shared"
        db_path = root / "logpile.db"
        source = (
            home
            / ".claude"
            / "projects"
            / "-Users-alice-demo"
            / "session-1.jsonl"
        )
        return home, shared, db_path, source

    def _sync_private(self, root: Path, body: str = "Clean publish candidate."):
        home, shared, db_path, source = self._paths(root)
        init_db(db_path)
        with _connect(db_path) as conn:
            ensure_user(conn, "alice", display_name="Alice")
            update_user(conn, "alice", default_session_visibility="private")
        _write_session(source, body)
        sync_sessions(shared, db_path, "alice", "machine-1", home)
        return home, shared, db_path, source

    def _approve_public(self, db_path: Path, shared: Path):
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
        return result

    def test_invalid_visibility_rejects_closed_and_public_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _home, shared, db_path, _source = self._sync_private(root)

            with (
                _connect(db_path) as conn,
                self.assertRaisesRegex(ValueError, "Unsupported visibility"),
            ):
                update_user(conn, "alice", default_session_visibility="typo-public")

            result = CliRunner().invoke(
                cli,
                [
                    "visibility",
                    "session-1",
                    "public",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                ],
            )
            self.assertEqual(result.exit_code, 1)
            self.assertIn("successful review", result.output)
            with _connect(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()
                self.assertEqual(row["visibility"], "private")

    def test_migration_repairs_invalid_stored_visibility_through_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _home, _shared, db_path, _source = self._sync_private(root)
            with _connect(db_path) as conn:
                conn.execute(
                    """
                    UPDATE sessions
                    SET visibility = 'typo-public', is_private = 0
                    WHERE session_id = 'session-1'
                    """
                )

            init_db(db_path)
            with _connect(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, is_private FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()
                transition = conn.execute(
                    """
                    SELECT from_visibility, to_visibility, transition_source
                    FROM visibility_transitions
                    WHERE session_id = 'session-1'
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
            self.assertEqual(tuple(row), ("private", 1))
            self.assertEqual(
                tuple(transition),
                ("typo-public", "private", "migration"),
            )

    def test_private_to_unlisted_warns_and_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _home, shared, db_path, _source = self._sync_private(root)
            result = CliRunner().invoke(
                cli,
                [
                    "visibility",
                    "session-1",
                    "unlisted",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("local/link artifacts", result.output)
            with _connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT from_visibility, to_visibility, warning
                    FROM visibility_transitions
                    WHERE session_id = 'session-1'
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
            self.assertEqual((row["from_visibility"], row["to_visibility"]), ("private", "unlisted"))
            self.assertIn("not served in public mode", row["warning"])

    def test_unlisted_approval_cannot_be_reused_as_public_approval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _home, shared, db_path, _source = self._sync_private(
                root,
                body="Contact reviewer@example.com before release.",
            )
            runner = CliRunner()
            unlisted = runner.invoke(
                cli,
                [
                    "publish",
                    "approve",
                    "session-1",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                ],
            )
            self.assertEqual(unlisted.exit_code, 0, unlisted.output)

            promotion = runner.invoke(
                cli,
                [
                    "visibility",
                    "session-1",
                    "public",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                ],
            )
            self.assertEqual(promotion.exit_code, 1)
            self.assertIn("successful review", promotion.output)

            forced = runner.invoke(
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
                    "--force",
                ],
            )
            self.assertEqual(forced.exit_code, 0, forced.output)
            with _connect(db_path) as conn:
                reviews = conn.execute(
                    """
                    SELECT approved_visibility, forced
                    FROM publication_reviews
                    WHERE session_id = 'session-1'
                    ORDER BY id
                    """
                ).fetchall()
                visibility = conn.execute(
                    "SELECT visibility FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()[0]
            self.assertEqual(
                [(row["approved_visibility"], row["forced"]) for row in reviews],
                [("unlisted", 0), ("public", 1)],
            )
            self.assertEqual(visibility, "public")

    def test_approval_persists_hash_artifact_and_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _home, shared, db_path, _source = self._sync_private(root)
            self._approve_public(db_path, shared)

            with _connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT visibility, reviewed_sha256, reviewed_artifact_path,
                           publication_review_id, publication_state, file_hash
                    FROM sessions WHERE session_id = 'session-1'
                    """
                ).fetchone()
                review = conn.execute(
                    "SELECT * FROM publication_reviews WHERE id = ?",
                    (row["publication_review_id"],),
                ).fetchone()
            artifact = Path(row["reviewed_artifact_path"])
            self.assertEqual(row["visibility"], "public")
            self.assertEqual(row["publication_state"], "reviewed")
            self.assertEqual(len(row["reviewed_sha256"]), 64)
            self.assertEqual(row["reviewed_sha256"], row["file_hash"])
            self.assertEqual(review["successful"], 1)
            self.assertTrue(artifact.is_relative_to(shared / ".published"))
            self.assertTrue(artifact.is_file())
            self.assertFalse(artifact.is_symlink())
            self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list((shared / ".review-staging").iterdir()), [])

    def test_source_drift_revokes_public_and_never_exposes_new_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, shared, db_path, source = self._sync_private(root)
            self._approve_public(db_path, shared)
            with _connect(db_path) as conn:
                reviewed_path = Path(
                    conn.execute(
                        "SELECT reviewed_artifact_path FROM sessions WHERE session_id = 'session-1'"
                    ).fetchone()[0]
                )
            reviewed_bytes = reviewed_path.read_bytes()

            public_client = create_app(db_path, shared, public_mode=True).test_client()
            self.assertEqual(public_client.get("/sessions/session-1").status_code, 200)

            _write_session(source, "DRIFT-METADATA-SENTINEL")
            sync_sessions(shared, db_path, "alice", "machine-1", home)

            with _connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT visibility, publication_state, reviewed_artifact_path,
                           visibility_reason
                    FROM sessions WHERE session_id = 'session-1'
                    """
                ).fetchone()
            self.assertEqual(row["visibility"], "unlisted")
            self.assertEqual(row["publication_state"], "source_drift")
            self.assertIn("revoked and requeued", row["visibility_reason"])
            self.assertEqual(Path(row["reviewed_artifact_path"]).read_bytes(), reviewed_bytes)
            response = create_app(db_path, shared, public_mode=True).test_client().get(
                "/sessions/session-1"
            )
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(b"DRIFT-METADATA-SENTINEL", response.data)

            self._approve_public(db_path, shared)
            with _connect(db_path) as conn:
                republished = conn.execute(
                    """
                    SELECT visibility, publication_state, reviewed_artifact_path
                    FROM sessions WHERE session_id = 'session-1'
                    """
                ).fetchone()
            self.assertEqual(republished["visibility"], "public")
            self.assertEqual(republished["publication_state"], "reviewed")
            self.assertNotEqual(
                Path(republished["reviewed_artifact_path"]),
                reviewed_path,
            )
            self.assertEqual(reviewed_path.read_bytes(), reviewed_bytes)
            response = create_app(db_path, shared, public_mode=True).test_client().get(
                "/sessions/session-1"
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"DRIFT-METADATA-SENTINEL", response.data)

    def test_rotated_only_parser_backfill_revokes_stale_public_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, shared, db_path, source = self._sync_private(
                root,
                body="Canonical rotated transcript title.",
            )
            with _connect(db_path) as conn:
                conn.execute(
                    """
                    UPDATE sessions
                    SET first_user_message = 'STALE PARSER TITLE',
                        token_version = 0
                    WHERE session_id = 'session-1'
                    """
                )
            self._approve_public(db_path, shared)

            with _connect(db_path) as conn:
                before = conn.execute(
                    """
                    SELECT reviewed_artifact_path, reviewed_metadata_sha256
                    FROM sessions WHERE session_id = 'session-1'
                    """
                ).fetchone()
            reviewed_path = Path(before["reviewed_artifact_path"])
            reviewed_bytes = reviewed_path.read_bytes()
            source.unlink()

            result = sync_sessions(shared, db_path, "alice", "machine-1", home)

            self.assertEqual(result.updated, 1)
            with _connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT visibility, publication_state, first_user_message,
                           publication_metadata_sha256,
                           reviewed_metadata_sha256, visibility_reason
                    FROM sessions WHERE session_id = 'session-1'
                    """
                ).fetchone()
            self.assertEqual(row["visibility"], "unlisted")
            self.assertEqual(row["publication_state"], "source_drift")
            self.assertEqual(
                row["first_user_message"],
                "Canonical rotated transcript title.",
            )
            self.assertNotEqual(
                row["publication_metadata_sha256"],
                row["reviewed_metadata_sha256"],
            )
            self.assertIn("rotated transcript metadata drifted", row["visibility_reason"])
            self.assertEqual(reviewed_path.read_bytes(), reviewed_bytes)
            response = create_app(
                db_path,
                shared,
                public_mode=True,
            ).test_client().get("/sessions/session-1")
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(b"STALE PARSER TITLE", response.data)

    def test_same_size_restored_mtime_public_rewrite_is_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, shared, db_path, source = self._sync_private(
                root,
                body="Clean publish candidate.",
            )
            self._approve_public(db_path, shared)
            original_stat = source.stat()
            original = source.read_bytes()
            changed = original.replace(
                b"Clean publish candidate.",
                b"DRIFT publish candidate.",
            )
            self.assertEqual(len(changed), len(original))
            source.write_bytes(changed)
            os.utime(
                source,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )

            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with _connect(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, publication_state FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()
            self.assertEqual(row["visibility"], "unlisted")
            self.assertEqual(row["publication_state"], "source_drift")

    def test_same_transcript_metadata_drift_revokes_before_public_render(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _home, shared, db_path, _source = self._sync_private(root)
            self._approve_public(db_path, shared)
            with _connect(db_path) as conn:
                original_hash = conn.execute(
                    "SELECT file_hash FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()[0]
                conn.execute(
                    """
                    UPDATE sessions
                    SET session_goal = 'UNREVIEWED METADATA SENTINEL'
                    WHERE session_id = 'session-1'
                    """
                )

            response = create_app(
                db_path,
                shared,
                public_mode=True,
            ).test_client().get("/sessions/session-1")
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(b"UNREVIEWED METADATA SENTINEL", response.data)
            with _connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT visibility, publication_state, file_hash
                    FROM sessions WHERE session_id = 'session-1'
                    """
                ).fetchone()
            self.assertEqual(row["file_hash"], original_hash)
            self.assertEqual(row["visibility"], "unlisted")
            self.assertEqual(row["publication_state"], "source_drift")

    def test_profile_metadata_drift_revokes_sessions_and_hides_profile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _home, shared, db_path, _source = self._sync_private(root)
            with _connect(db_path) as conn:
                update_user(
                    conn,
                    "alice",
                    bio="Reviewed operator biography",
                    avatar_url="https://example.invalid/reviewed-avatar.png",
                )
            self._approve_public(db_path, shared)
            client = create_app(db_path, shared, public_mode=True).test_client()
            self.assertEqual(client.get("/sessions/session-1").status_code, 200)
            self.assertEqual(client.get("/u/alice").status_code, 200)
            reviewed_profile = client.get("/api/users/alice")
            self.assertEqual(reviewed_profile.status_code, 200)
            self.assertEqual(
                reviewed_profile.get_json()["user"]["bio"],
                "Reviewed operator biography",
            )

            injected = "sk-proj-" + ("A" * 32)
            with _connect(db_path) as conn:
                update_user(
                    conn,
                    "alice",
                    display_name=injected,
                    bio=f"unreviewed profile credential {injected}",
                    avatar_url=f"https://alice:{injected}@example.invalid/avatar",
                )
                row = conn.execute(
                    """
                    SELECT visibility, publication_state,
                           publication_metadata_sha256,
                           reviewed_metadata_sha256, visibility_reason
                    FROM sessions WHERE session_id = 'session-1'
                    """
                ).fetchone()
                transition = conn.execute(
                    """
                    SELECT transition_source, from_visibility, to_visibility
                    FROM visibility_transitions
                    WHERE session_id = 'session-1'
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()

            self.assertEqual(row["visibility"], "unlisted")
            self.assertEqual(row["publication_state"], "source_drift")
            self.assertNotEqual(
                row["publication_metadata_sha256"],
                row["reviewed_metadata_sha256"],
            )
            self.assertIn("user metadata drifted", row["visibility_reason"])
            self.assertEqual(
                tuple(transition),
                ("drift", "public", "unlisted"),
            )
            response = client.get("/sessions/session-1")
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(injected.encode(), response.data)
            self.assertEqual(client.get("/u/alice").status_code, 404)
            self.assertEqual(client.get("/api/users/alice").status_code, 404)
            people_response = client.get("/u")
            users_response = client.get("/api/users")
            self.assertNotIn(injected.encode(), people_response.data)
            self.assertNotIn(injected.encode(), users_response.data)
            listed_users = users_response.get_json()
            self.assertFalse(
                any(user["username"] == "alice" for user in listed_users),
                listed_users,
            )

    def test_reviewed_profile_metadata_allows_flask_public_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _home, shared, db_path, _source = self._sync_private(root)
            with _connect(db_path) as conn:
                update_user(
                    conn,
                    "alice",
                    bio="Reviewed operator biography.",
                    avatar_url="https://example.invalid/alice.png",
                )
            self._approve_public(db_path, shared)

            client = create_app(db_path, shared, public_mode=True).test_client()
            self.assertEqual(client.get("/sessions/session-1").status_code, 200)
            profile = client.get("/u/alice")
            self.assertEqual(profile.status_code, 200)
            self.assertIn(b"Reviewed operator biography.", profile.data)

    def test_public_reader_rejects_symlinked_shared_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _home, shared, db_path, _source = self._sync_private(root)
            self._approve_public(db_path, shared)
            real_shared = root / "real-shared"
            shared.rename(real_shared)
            shared.symlink_to(real_shared, target_is_directory=True)

            response = create_app(
                db_path,
                shared,
                public_mode=True,
            ).test_client().get("/sessions/session-1")
            self.assertEqual(response.status_code, 404)

    def test_public_mode_rejects_corrupt_or_symlinked_reviewed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _home, shared, db_path, source = self._sync_private(root)
            self._approve_public(db_path, shared)
            with _connect(db_path) as conn:
                artifact = Path(
                    conn.execute(
                        "SELECT reviewed_artifact_path FROM sessions WHERE session_id = 'session-1'"
                    ).fetchone()[0]
                )

            artifact.write_bytes(b"corrupt")
            client = create_app(db_path, shared, public_mode=True).test_client()
            self.assertEqual(client.get("/sessions/session-1").status_code, 404)

            artifact.unlink()
            artifact.symlink_to(source)
            self.assertEqual(client.get("/sessions/session-1").status_code, 404)

    def test_verified_public_reader_stays_bound_to_open_artifact_fd(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _home, shared, db_path, source = self._sync_private(
                root,
                body="ORIGINAL REVIEWED SENTINEL",
            )
            self._approve_public(db_path, shared)
            with _connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT s.*, COALESCE(u.display_name, u.username, s.username)
                        AS user_display_name
                    FROM sessions s
                    LEFT JOIN users u ON u.username = s.username
                    WHERE s.session_id = 'session-1'
                    """
                ).fetchone()
            artifact = Path(row["reviewed_artifact_path"])
            moved = artifact.with_suffix(".verified-open")

            with open_verified_public_artifact(
                row,
                shared_dir=shared,
            ) as artifact_stream:
                self.assertIsNotNone(artifact_stream)
                artifact.rename(moved)
                _write_session(source, "UNREVIEWED SWAP SENTINEL")
                artifact.symlink_to(source)
                turns = render_claudecode_transcript(artifact_stream)

            rendered = json.dumps(turns)
            self.assertIn("ORIGINAL REVIEWED SENTINEL", rendered)
            self.assertNotIn("UNREVIEWED SWAP SENTINEL", rendered)

    def test_copy_mismatch_persists_retry_without_advancing_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, shared, db_path, source = self._sync_private(root)
            # Move into shared archival storage so the update exercises H1.
            result = CliRunner().invoke(
                cli,
                [
                    "visibility",
                    "session-1",
                    "unlisted",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            with _connect(db_path) as conn:
                before = conn.execute(
                    "SELECT file_hash, file_mtime FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()

            _write_session(source, "new revision")

            def corrupt_copy(_src, dst, **_kwargs):
                Path(dst).write_bytes(b"corrupt archival copy")
                os.chmod(dst, 0o600)

            with mock.patch("logpile.sync._secure_copy_file", side_effect=corrupt_copy):
                sync_sessions(shared, db_path, "alice", "machine-1", home)

            with _connect(db_path) as conn:
                after = conn.execute(
                    "SELECT file_hash, file_mtime FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()
                retry = conn.execute(
                    "SELECT * FROM sync_copy_retries WHERE session_id = 'session-1'"
                ).fetchone()
            self.assertEqual(after["file_hash"], before["file_hash"])
            self.assertEqual(after["file_mtime"], before["file_mtime"])
            self.assertIsNotNone(retry)
            self.assertIn("hash mismatch", retry["last_error"])

            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with _connect(db_path) as conn:
                final_hash = conn.execute(
                    "SELECT file_hash FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()[0]
                retry_count = conn.execute(
                    "SELECT COUNT(*) FROM sync_copy_retries"
                ).fetchone()[0]
            self.assertNotEqual(final_hash, before["file_hash"])
            self.assertEqual(retry_count, 0)

    def test_legacy_private_row_without_archive_is_planned_and_healed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, shared, db_path, source = self._sync_private(root)
            with _connect(db_path) as conn:
                archive = Path(
                    conn.execute(
                        "SELECT shared_path FROM sessions WHERE session_id = 'session-1'"
                    ).fetchone()[0]
                )
                archive.unlink()
                conn.execute(
                    "UPDATE sessions SET shared_path = '' WHERE session_id = 'session-1'"
                )
                before = conn.execute(
                    """
                    SELECT source_path, file_hash, file_size, file_mtime
                    FROM sessions WHERE session_id = 'session-1'
                    """
                ).fetchone()
                before_metadata = tuple(before)

            def corrupt_copy(_src, dst, **_kwargs):
                Path(dst).write_bytes(b"corrupt archival copy")
                os.chmod(dst, 0o600)

            first_stderr = io.StringIO()
            with (
                mock.patch("logpile.sync._secure_copy_file", side_effect=corrupt_copy),
                contextlib.redirect_stderr(first_stderr),
            ):
                sync_sessions(shared, db_path, "alice", "machine-1", home)

            self.assertIn("Archival shared-copy preflight plans", first_stderr.getvalue())
            self.assertIn("across 1 transcript(s)", first_stderr.getvalue())
            with _connect(db_path) as conn:
                failed = conn.execute(
                    """
                    SELECT source_path, file_hash, file_size, file_mtime, shared_path
                    FROM sessions WHERE session_id = 'session-1'
                    """
                ).fetchone()
                retry_count = conn.execute(
                    "SELECT COUNT(*) FROM sync_copy_retries WHERE session_id = 'session-1'"
                ).fetchone()[0]
            self.assertEqual(tuple(failed)[:4], before_metadata)
            self.assertEqual(failed["shared_path"], "")
            self.assertEqual(retry_count, 1)

            second_stderr = io.StringIO()
            with contextlib.redirect_stderr(second_stderr):
                sync_sessions(shared, db_path, "alice", "machine-1", home)
            self.assertIn("Archival shared-copy preflight plans", second_stderr.getvalue())
            with _connect(db_path) as conn:
                healed = conn.execute(
                    """
                    SELECT source_path, file_hash, file_size, file_mtime, shared_path
                    FROM sessions WHERE session_id = 'session-1'
                    """
                ).fetchone()
                retry_count = conn.execute(
                    "SELECT COUNT(*) FROM sync_copy_retries WHERE session_id = 'session-1'"
                ).fetchone()[0]

            self.assertEqual(tuple(healed)[:4], before_metadata)
            healed_archive = Path(healed["shared_path"])
            private_root = shared.parent / f".{shared.name}-private"
            self.assertTrue(healed_archive.is_relative_to(private_root))
            self.assertTrue(healed_archive.is_file())
            self.assertFalse(healed_archive.is_symlink())
            self.assertEqual(healed_archive.stat().st_mode & 0o777, 0o600)
            self.assertEqual(healed_archive.read_bytes(), source.read_bytes())
            self.assertEqual(retry_count, 0)

    def test_free_space_preflight_reports_plan_and_starts_no_copy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, shared, db_path, source = self._paths(root)
            _write_session(source, "planned copy")
            usage = namedtuple("usage", "total used free")(100, 100, 0)
            stderr = io.StringIO()
            with (
                mock.patch("logpile.sync.shutil.disk_usage", return_value=usage),
                contextlib.redirect_stderr(stderr),
                self.assertRaises(StorageSafetyError),
            ):
                sync_sessions(shared, db_path, "alice", "machine-1", home)
            self.assertIn("preflight plans", stderr.getvalue())
            self.assertIn("0.0 B free", stderr.getvalue())
            with _connect(db_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
