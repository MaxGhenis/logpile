import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from logpile.cli import cli
from logpile.db import ensure_user, init_db, set_session_visibility, update_user
from logpile.origins import derive_session_origin
from logpile.sync import SESSION_TOKEN_VERSION, sync_sessions
from logpile.web.app import create_app


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


class SyncTests(unittest.TestCase):
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
            self.assertEqual(shared_path, "")
            self.assertFalse(copied_path.exists())

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
            self.assertEqual(row["shared_path"], "")

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
            self.assertEqual(row["shared_path"], "")

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
            self.assertEqual(row["shared_path"], "")

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
            self.assertFalse(shared.exists())

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

            self.assertEqual(user[0], "alice-smith")
            self.assertEqual(user[1], "Alice Smith")
            self.assertEqual(session[0], "alice-smith")
            self.assertEqual(session[1], "public")

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

            self.assertEqual((rows[0]["session_id"], rows[0]["visibility"]), ("session-1", "public"))
            self.assertEqual((rows[1]["session_id"], rows[1]["visibility"]), ("session-2", "unlisted"))


# --- archive-integrity tests -------------------------------------------------
# Imports are grouped here (not at top of file) so this block stays a single
# self-contained appended hunk.
import errno as _errno  # noqa: E402
import fcntl as _fcntl  # noqa: E402
import shutil as _shutil  # noqa: E402
from unittest import mock as _mock  # noqa: E402

from logpile import sync as _sync_module  # noqa: E402
from logpile.sync import _copy_session  # noqa: E402


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
        self.assertEqual(self._tmp_leftovers(), [])

    def test_skips_identical_existing_copy(self) -> None:
        _shutil.copy2(self.src, self.dst)
        self.assertFalse(_copy_session(self.src, self.dst))

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
            Path(dst).write_text("partial")
            raise OSError(_errno.EIO, "boom")

        with _mock.patch.object(_sync_module.shutil, "copy2", side_effect=boom):
            with self.assertRaises(OSError):
                _copy_session(self.src, self.dst)
        self.assertEqual(self.dst.read_text(), "previous complete copy\n")
        self.assertEqual(self._tmp_leftovers(), [])

    def test_enospc_keeps_existing_complete_copy_over_symlink(self) -> None:
        self.dst.write_text("previous complete copy\n")

        def full(src, dst):
            raise OSError(_errno.ENOSPC, "disk full")

        with _mock.patch.object(_sync_module.shutil, "copy2", side_effect=full):
            self.assertFalse(_copy_session(self.src, self.dst))
        self.assertFalse(self.dst.is_symlink())
        self.assertEqual(self.dst.read_text(), "previous complete copy\n")

    def test_enospc_without_existing_copy_falls_back_to_symlink(self) -> None:
        def full(src, dst):
            raise OSError(_errno.ENOSPC, "disk full")

        with _mock.patch.object(_sync_module.shutil, "copy2", side_effect=full):
            self.assertTrue(_copy_session(self.src, self.dst))
        self.assertTrue(self.dst.is_symlink())
        self.assertEqual(self.dst.resolve(), self.src.resolve())


class SyncCoverageAndFastPathTests(unittest.TestCase):
    """Multi-root codex discovery, per-day usage rows, and the size+mtime
    fast path that keeps 20GB+ archives from being re-hashed every sync."""

    # Reuse the session-writing fixtures without re-running SyncTests' cases.
    _write_claude_session = SyncTests._write_claude_session
    _write_codex_session = SyncTests._write_codex_session

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
            # Skipped before init_db: no database was created.
            self.assertFalse(db_path.exists())


if __name__ == "__main__":
    unittest.main()
