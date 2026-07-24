import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from logpile.db import (
    ensure_user,
    get_db,
    init_db,
    transition_session_visibility,
    update_user,
    upsert_session,
)
from logpile.publish import publication_metadata_sha256


def open_sqlite(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return closing(conn)


def make_session(session_id: str, *, username: str, visibility: str) -> dict:
    return {
        "session_id": session_id,
        "source": "claudecode",
        "username": username,
        "machine": "machine-1",
        "project": "demo",
        "workspace_root": "/tmp/demo",
        "worktree_root": "/tmp/demo",
        "repo_root": "/tmp/demo",
        "repo_name": "demo",
        "git_branch": "main",
        "git_commit": "abc123",
        "git_dirty": 0,
        "source_path": f"/tmp/{session_id}.jsonl",
        "shared_path": f"/shared/{session_id}.jsonl",
        "first_timestamp": "2026-04-11T12:00:00+00:00",
        "last_timestamp": "2026-04-11T12:05:00+00:00",
        "duration_seconds": 300,
        "user_message_count": 1,
        "assistant_message_count": 1,
        "tool_call_count": 0,
        "error_count": 0,
        "write_path_count": 0,
        "read_path_count": 0,
        "search_path_count": 0,
        "test_run_count": 0,
        "test_failure_count": 0,
        "lint_run_count": 0,
        "lint_failure_count": 0,
        "build_run_count": 0,
        "build_failure_count": 0,
        "format_run_count": 0,
        "format_failure_count": 0,
        "git_status_count": 0,
        "git_diff_count": 0,
        "git_commit_count": 0,
        "activity_version": 1,
        "total_input_tokens": 10,
        "total_output_tokens": 20,
        "first_user_message": "hello",
        "visibility": visibility,
        "visibility_source": "default",
        "visibility_rule_id": None,
        "visibility_reason": f"default:{visibility}",
        "is_private": 1 if visibility == "private" else 0,
        "file_hash": "a" * 64,
        "synced_at": "2026-04-11T12:05:00+00:00",
        "model": "claude-3.7",
    }


class ContractViewTests(unittest.TestCase):
    def test_session_catalog_exposes_public_and_direct_flags(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)

            with get_db(db_path) as conn:
                username = ensure_user(conn, "alice")
                update_user(conn, username, profile_visibility="unlisted")
                with self.assertWarnsRegex(
                    RuntimeWarning, "kept this session unlisted"
                ):
                    upsert_session(
                        conn,
                        make_session(
                            "public-1", username=username, visibility="public"
                        ),
                    )
                public_row = conn.execute(
                    "SELECT * FROM sessions WHERE session_id = 'public-1'"
                ).fetchone()
                metadata_sha256 = publication_metadata_sha256(public_row)
                review_id = conn.execute(
                    """
                    INSERT INTO publication_reviews (
                        session_id, reviewed_sha256, reviewed_artifact_path,
                        reviewed_metadata_sha256,
                        recommendation, approved_visibility, forced,
                        successful, reviewed_at
                    ) VALUES (?, ?, ?, ?, 'public', 'public', 0, 1, ?)
                    """,
                    (
                        "public-1",
                        "a" * 64,
                        "/shared/.published/public-1/artifact.jsonl",
                        metadata_sha256,
                        "2026-04-11T12:05:00+00:00",
                    ),
                ).lastrowid
                conn.execute(
                    """
                    UPDATE sessions
                    SET reviewed_sha256 = ?, reviewed_artifact_path = ?,
                        publication_metadata_sha256 = ?,
                        reviewed_metadata_sha256 = ?, publication_review_id = ?
                    WHERE session_id = 'public-1'
                    """,
                    (
                        "a" * 64,
                        "/shared/.published/public-1/artifact.jsonl",
                        metadata_sha256,
                        metadata_sha256,
                        review_id,
                    ),
                )
                transition_session_visibility(
                    conn,
                    "public-1",
                    "public",
                    shared_dir=None,
                    transition_source="review",
                    reason="contract fixture reviewed publication",
                    publication_review_id=review_id,
                    manage_storage=False,
                )
                upsert_session(
                    conn,
                    make_session(
                        "unlisted-1", username=username, visibility="unlisted"
                    ),
                )
                upsert_session(
                    conn,
                    make_session("private-1", username=username, visibility="private"),
                )

            with open_sqlite(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT session_id, listed_public, listed_private, direct_public, direct_private
                    FROM session_catalog
                    ORDER BY session_id
                    """
                ).fetchall()

            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    ("private-1", 0, 0, 0, 0),
                    ("public-1", 0, 1, 1, 1),
                    ("unlisted-1", 0, 1, 0, 1),
                ],
            )

    def test_user_catalog_exposes_listed_and_direct_profile_flags(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)

            with get_db(db_path) as conn:
                public_username = ensure_user(conn, "alice")
                unlisted_username = ensure_user(conn, "bob")
                private_username = ensure_user(conn, "carol")
                update_user(conn, unlisted_username, profile_visibility="unlisted")
                update_user(conn, private_username, profile_visibility="private")

            with open_sqlite(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT username, listed_public, listed_private, direct_public, direct_private
                    FROM user_catalog
                    ORDER BY username
                    """
                ).fetchall()

            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    (public_username, 1, 1, 1, 1),
                    (unlisted_username, 0, 1, 1, 1),
                    (private_username, 0, 1, 0, 1),
                ],
            )
