import sqlite3
import tempfile
import unittest
from pathlib import Path

from logpile.db import init_db

FIXTURES = Path(__file__).parent / "fixtures" / "migrations"


class IdentityMigrationTests(unittest.TestCase):
    def test_collision_rebuild_preserves_every_nonidentity_column_and_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "legacy.db"
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    (FIXTURES / "legacy-identity-collision.sql").read_text(
                        encoding="utf-8"
                    )
                )

            init_db(db_path)

            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                user_columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(users)").fetchall()
                }
                session_columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
                }
                rule_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(session_visibility_rules)"
                    ).fetchall()
                }
                users = conn.execute(
                    """
                    SELECT username, display_name, github_username, custom_profile_note
                    FROM users ORDER BY username
                    """
                ).fetchall()
                sessions = conn.execute(
                    """
                    SELECT session_id, username, cache_creation_input_tokens,
                           native_total_output_tokens, custom_session_note
                    FROM sessions ORDER BY session_id
                    """
                ).fetchall()
                rules = conn.execute(
                    """
                    SELECT username, custom_rule_note
                    FROM session_visibility_rules ORDER BY id
                    """
                ).fetchall()
                github_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(user_github_daily)"
                    ).fetchall()
                }
                github_rows = conn.execute(
                    """
                    SELECT username, day, contributions, commits, custom_github_note
                    FROM user_github_daily ORDER BY username, day
                    """
                ).fetchall()
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

            self.assertNotIn("slug", user_columns)
            self.assertNotIn("user_slug", session_columns)
            self.assertNotIn("user_slug", rule_columns)
            self.assertIn("github_username", user_columns)
            self.assertIn("custom_profile_note", user_columns)
            self.assertIn("custom_session_note", session_columns)
            self.assertIn("custom_rule_note", rule_columns)
            self.assertIn("custom_github_note", github_columns)
            self.assertEqual(
                [tuple(row) for row in users],
                [
                    ("alice", "Upper Alice", "AliceUpper", "upper-note"),
                    ("alice-2", "Lower Alice", "aliceLower", "lower-note"),
                ],
            )
            self.assertEqual(
                [tuple(row) for row in sessions],
                [
                    ("session-lower", "alice-2", 32, 22, "lower-session-note"),
                    ("session-upper", "alice", 31, 11, "upper-session-note"),
                ],
            )
            self.assertEqual(
                [tuple(row) for row in rules],
                [("alice", "upper-rule-note"), ("alice-2", "lower-rule-note")],
            )
            self.assertEqual(
                [tuple(row) for row in github_rows],
                [
                    ("alice", "2025-01-01", 11, 7, "upper-github-note"),
                    ("alice-2", "2025-01-01", 22, 9, "lower-github-note"),
                ],
            )

            snapshots = list(
                db_path.parent.glob(f"{db_path.name}.pre-identity-migration.sqlite*")
            )
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0].stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(snapshots[0]) as snapshot:
                snapshot_user_columns = {
                    row[1]
                    for row in snapshot.execute("PRAGMA table_info(users)").fetchall()
                }
                self.assertIn("slug", snapshot_user_columns)
                self.assertEqual(
                    snapshot.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2
                )
                self.assertEqual(
                    snapshot.execute("PRAGMA quick_check").fetchone()[0], "ok"
                )

            # The legacy marker columns are gone, so an idempotent migration
            # must not create another destructive-rebuild snapshot.
            init_db(db_path)
            self.assertEqual(
                len(
                    list(
                        db_path.parent.glob(
                            f"{db_path.name}.pre-identity-migration.sqlite*"
                        )
                    )
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
