import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

from logpile.cli import cli
from logpile.db import create_visibility_rule, ensure_user, init_db, set_session_visibility, update_user
from logpile.objectives import normalize_objective_family
from logpile.sync import sync_sessions
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


class WebAppTests(unittest.TestCase):
    def _prepare_user(
        self,
        db_path: Path,
        *,
        username: str = "alice",
        profile_visibility: str = "public",
        default_session_visibility: str = "unlisted",
    ) -> None:
        init_db(db_path)
        with open_sqlite(db_path) as conn:
            ensure_user(conn, username, display_name=username)
            update_user(
                conn,
                username,
                profile_visibility=profile_visibility,
                default_session_visibility=default_session_visibility,
            )
            conn.commit()

    def _write_claude_session(
        self,
        home: Path,
        *,
        session_id: str = "session-1",
        message: str = "hello world",
        assistant_content: list[dict] | None = None,
        cwd: str = "/tmp/demo",
    ) -> None:
        # Relative to now so fixtures stay inside the rolling 30-day windows
        # the chart/analysis endpoints query (a fixed date silently ages out).
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        write_jsonl(
            home / ".claude" / "projects" / "-Users-alice-demo" / f"{session_id}.jsonl",
            [
                {
                    "timestamp": recent.isoformat().replace("+00:00", "Z"),
                    "type": "user",
                    "cwd": cwd,
                    "message": {"content": message},
                },
                {
                    "timestamp": (recent + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
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

    def _write_codex_session(
        self,
        home: Path,
        *,
        session_id: str,
        message: str,
        timestamp: datetime,
        cwd: str = "/tmp/demo",
        parent_session_id: str | None = None,
        spawn_depth: int = 0,
        total_input_tokens: int = 1200,
        cached_input_tokens: int = 300,
        total_output_tokens: int = 45,
    ) -> None:
        session_path = (
            home
            / ".codex"
            / "sessions"
            / timestamp.strftime("%Y")
            / timestamp.strftime("%m")
            / timestamp.strftime("%d")
            / f"{session_id}.jsonl"
        )
        meta_payload = {
            "id": session_id,
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "cwd": cwd,
            "originator": "Codex Desktop",
        }
        if parent_session_id:
            meta_payload["source"] = {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": parent_session_id,
                        "depth": spawn_depth,
                    }
                }
            }
        write_jsonl(
            session_path,
            [
                {
                    "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                    "type": "session_meta",
                    "payload": meta_payload,
                },
                {
                    "timestamp": (timestamp + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": message}],
                    },
                },
                {
                    "timestamp": (timestamp + timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
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

    def _seed_user(
        self,
        *,
        root: Path,
        username: str = "alice",
        session_id: str = "session-1",
        message: str = "hello world",
        assistant_content: list[dict] | None = None,
        cwd: str = "/tmp/demo",
    ) -> tuple[Path, Path]:
        home = root / username
        shared = root / "shared"
        db_path = root / "logpile.db"
        self._prepare_user(db_path, username=username)
        self._write_claude_session(
            home,
            session_id=session_id,
            message=message,
            assistant_content=assistant_content,
            cwd=cwd,
        )
        sync_sessions(
            shared_dir=shared,
            db_path=db_path,
            username=username,
            machine="machine-1",
            home=home,
        )
        self._approve_public(db_path, shared, session_id)
        return shared, db_path

    def _approve_public(self, db_path: Path, shared: Path, session_id: str) -> None:
        result = CliRunner().invoke(
            cli,
            [
                "publish",
                "approve",
                session_id,
                "--db",
                str(db_path),
                "--shared",
                str(shared),
                "--visibility",
                "public",
            ],
        )
        self.assertEqual(
            result.exit_code,
            0,
            f"{result.output}\n{result.exception!r}",
        )

    def _seed_mixed_visibility_codex_lineage(self, root: Path) -> tuple[Path, Path]:
        home = root / "alice"
        shared = root / "shared"
        db_path = root / "logpile.db"
        self._prepare_user(db_path, username="alice")
        now = datetime.now(timezone.utc) - timedelta(days=1)
        self._write_codex_session(
            home,
            session_id="private-root",
            message="PRIVATE ROOT SENTINEL: never expose this lineage goal",
            timestamp=now,
            total_input_tokens=100_000_000,
            cached_input_tokens=90_000_000,
            total_output_tokens=5_000_000,
        )
        self._write_codex_session(
            home,
            session_id="public-child-a",
            message="Public child A",
            timestamp=now + timedelta(minutes=5),
            parent_session_id="private-root",
            spawn_depth=1,
            total_input_tokens=400_000_000,
            cached_input_tokens=380_000_000,
            total_output_tokens=15_000_000,
        )
        self._write_codex_session(
            home,
            session_id="public-child-b",
            message="Public child B",
            timestamp=now + timedelta(minutes=10),
            parent_session_id="private-root",
            spawn_depth=2,
            total_input_tokens=350_000_000,
            cached_input_tokens=330_000_000,
            total_output_tokens=10_000_000,
        )
        sync_sessions(
            shared_dir=shared,
            db_path=db_path,
            username="alice",
            machine="machine-1",
            home=home,
        )
        self._approve_public(db_path, shared, "public-child-a")
        self._approve_public(db_path, shared, "public-child-b")
        with open_sqlite(db_path) as conn:
            set_session_visibility(
                conn,
                "private-root",
                "private",
                shared_dir=shared,
            )
            conn.commit()
        return shared, db_path

    def _init_git_repo(self, root: Path) -> tuple[Path, str]:
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
        return repo, branch

    def test_private_profiles_are_hidden_from_public_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(root=root)

            with open_sqlite(db_path) as conn:
                update_user(conn, "alice", profile_visibility="private")
                conn.commit()

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            with app.test_client() as client:
                self.assertEqual(client.get("/u/alice").status_code, 404)
                self.assertEqual(client.get("/api/users/alice").status_code, 404)
                self.assertEqual(client.get("/api/users/alice/stats").status_code, 404)
                self.assertEqual(client.get("/api/users/alice/sessions").status_code, 404)
                self.assertEqual(client.get("/api/sessions").get_json(), [])
                sessions_page = client.get("/sessions")
                self.assertEqual(sessions_page.status_code, 200)
                self.assertNotIn(b"hello world", sessions_page.data)
                self.assertEqual(client.get("/sessions/session-1").status_code, 404)

    def test_unlisted_sessions_are_not_served_in_public_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(root=root)

            with open_sqlite(db_path) as conn:
                set_session_visibility(conn, "session-1", "unlisted", shared_dir=shared)
                conn.commit()

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            with app.test_client() as client:
                profile = client.get("/u/alice")
                profile_json = client.get("/api/users/alice")
                sessions_json = client.get("/api/users/alice/sessions")
                detail = client.get("/sessions/session-1")

                self.assertEqual(profile.status_code, 404)
                self.assertEqual(profile_json.status_code, 404)
                self.assertEqual(sessions_json.status_code, 404)
                self.assertEqual(detail.status_code, 404)
                self.assertNotIn(b"hello world", detail.data)

    def test_user_sessions_api_omits_message_preview(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(root=root)

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            with app.test_client() as client:
                payload = client.get("/api/users/alice/sessions").get_json()

            self.assertEqual(payload["total"], 1)
            self.assertNotIn("first_user_message", payload["sessions"][0])

    def test_invalid_pagination_params_return_400(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(root=root)

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            with app.test_client() as client:
                self.assertEqual(client.get("/sessions?page=x").status_code, 400)
                self.assertEqual(client.get("/api/users/alice/sessions?limit=foo").status_code, 400)
                self.assertEqual(client.get("/api/users/alice/sessions?offset=foo").status_code, 400)

    def test_sessions_page_invalid_activity_filter_is_inline_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(root=root)

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            with app.test_client() as client:
                response = client.get("/sessions?activity=unknown")

            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Invalid activity filter", response.data)
            self.assertIn(b"Unknown activity filter", response.data)

    def test_origin_filters_distinguish_direct_and_pipeline_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "alice"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_claude_session(home, session_id="direct-1", message="make more progress on logpile")
            self._write_claude_session(
                home,
                session_id="eval-1",
                message=(
                    "You are a senior statutory-fidelity reviewer for RAC (Rules as Code) encodings.\n\n"
                    "Review the file holistically for citation fidelity."
                ),
            )
            self._prepare_user(db_path, username="alice")
            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )
            app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)
            with app.test_client() as client:
                pipeline = client.get("/api/sessions?origin=pipeline_eval")
                direct = client.get("/api/users/alice/sessions?origin=human_direct")
                invalid = client.get("/api/sessions?origin=bogus")

            self.assertEqual(pipeline.status_code, 200)
            self.assertEqual(direct.status_code, 200)
            self.assertEqual(invalid.status_code, 400)

            pipeline_rows = pipeline.get_json()
            direct_payload = direct.get_json()
            self.assertEqual(len(pipeline_rows), 1)
            self.assertEqual(pipeline_rows[0]["session_id"], "eval-1")
            self.assertEqual(pipeline_rows[0]["session_origin"], "pipeline_eval")
            self.assertEqual(direct_payload["total"], 1)
            self.assertEqual(direct_payload["sessions"][0]["session_id"], "direct-1")
            self.assertEqual(direct_payload["sessions"][0]["session_origin"], "human_direct")

    def test_dashboard_defaults_to_human_direct_and_profile_stats_accept_origin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "alice"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_claude_session(home, session_id="direct-1", message="make more progress on logpile")
            self._write_claude_session(
                home,
                session_id="eval-1",
                message=(
                    "You are a senior statutory-fidelity reviewer for RAC (Rules as Code) encodings.\n\n"
                    "Review the file holistically for citation fidelity."
                ),
            )
            self._prepare_user(db_path, username="alice")
            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )
            app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)
            with app.test_client() as client:
                dashboard = client.get("/")
                pipeline_dashboard = client.get("/?origin=pipeline_eval")
                direct_profile = client.get("/api/users/alice?origin=human_direct").get_json()
                pipeline_profile = client.get("/api/users/alice/stats?origin=pipeline_eval").get_json()

            self.assertEqual(dashboard.status_code, 200)
            self.assertIn(b"make more progress on logpile", dashboard.data)
            self.assertNotIn(b"statutory-fidelity reviewer", dashboard.data)
            self.assertEqual(pipeline_dashboard.status_code, 200)
            self.assertIn(b"statutory-fidelity reviewer", pipeline_dashboard.data)
            self.assertEqual(direct_profile["summary"]["total_sessions"], 1)
            self.assertEqual(pipeline_profile["summary"]["total_sessions"], 1)
            self.assertEqual(pipeline_profile["summary"]["exploration_sessions"], 1)

    def test_analysis_defaults_to_human_direct_and_accepts_origin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "alice"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_claude_session(
                home,
                session_id="direct-1",
                message="make more progress on logpile",
                assistant_content=[
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": f"tool-{i}",
                        "input": {"command": "pytest -q"},
                    }
                    for i in range(220)
                ],
                cwd="/tmp/direct-app",
            )
            self._write_claude_session(
                home,
                session_id="eval-1",
                message=(
                    "You are a senior statutory-fidelity reviewer for RAC (Rules as Code) encodings.\n\n"
                    "Review the file holistically for citation fidelity."
                ),
                assistant_content=[
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": f"eval-tool-{i}",
                        "input": {"command": "python eval.py"},
                    }
                    for i in range(240)
                ],
                cwd="/tmp/eval-pipeline",
            )
            self._prepare_user(db_path, username="alice")
            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)
            with app.test_client() as client:
                analysis = client.get("/analysis")
                pipeline = client.get("/analysis?origin=pipeline_eval")

            self.assertEqual(analysis.status_code, 200)
            self.assertIn(b"pytest -q", analysis.data)
            self.assertNotIn(b"python eval.py", analysis.data)
            self.assertIn(b"direct-app", analysis.data)
            self.assertNotIn(b"eval-pipeline", analysis.data)
            self.assertEqual(pipeline.status_code, 200)
            self.assertIn(b"python eval.py", pipeline.data)
            self.assertIn(b"eval-pipeline", pipeline.data)
            self.assertNotIn(b"direct-app", pipeline.data)

    def test_analysis_surfaces_repeated_objective_relaunches_by_origin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "alice"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_claude_session(
                home,
                session_id="direct-1",
                message="Make more progress on Logpile analytics.",
                cwd="/tmp/direct-one",
            )
            self._write_claude_session(
                home,
                session_id="direct-2",
                message="Make more progress on Logpile analytics.",
                cwd="/tmp/direct-two",
            )
            self._write_claude_session(
                home,
                session_id="eval-1",
                message=(
                    "You are a senior statutory-fidelity reviewer for RAC (Rules as Code) encodings.\n\n"
                    "Review the file holistically for citation fidelity."
                ),
                cwd="/tmp/eval-one",
            )
            self._write_claude_session(
                home,
                session_id="eval-2",
                message=(
                    "You are a senior statutory-fidelity reviewer for RAC (Rules as Code) encodings.\n\n"
                    "Review the file holistically for citation fidelity."
                ),
                cwd="/tmp/eval-two",
            )
            self._prepare_user(db_path, username="alice")
            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)
            with app.test_client() as client:
                analysis = client.get("/analysis")
                pipeline = client.get("/analysis?origin=pipeline_eval")

            self.assertEqual(analysis.status_code, 200)
            self.assertIn(b"Make more progress on Logpile analytics.", analysis.data)
            self.assertIn(b"2 launches", analysis.data)
            self.assertNotIn(b"senior statutory-fidelity reviewer", analysis.data)

            self.assertEqual(pipeline.status_code, 200)
            self.assertIn(b"senior statutory-fidelity reviewer", pipeline.data)
            self.assertIn(b"2 launches", pipeline.data)
            self.assertNotIn(b"Make more progress on Logpile analytics.", pipeline.data)

    def test_analysis_surfaces_codex_context_explosions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "alice"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_user(db_path, username="alice")

            now = datetime.now(timezone.utc) - timedelta(days=1)
            self._write_codex_session(
                home,
                session_id="root-codex",
                message="check out mark sarneys email to me - the repo is crfb-tob-impacts",
                timestamp=now,
                cwd="/tmp/crfb-tob-impacts",
                # input_tokens includes the cached portion (Codex semantics):
                # 100M total input of which 90M is inherited/cached.
                total_input_tokens=100_000_000,
                cached_input_tokens=90_000_000,
                total_output_tokens=5_000_000,
            )
            self._write_codex_session(
                home,
                session_id="child-codex-a",
                message="Investigate the dashboard ratios.",
                timestamp=now + timedelta(minutes=5),
                cwd="/tmp/crfb-tob-impacts",
                parent_session_id="root-codex",
                spawn_depth=1,
                total_input_tokens=400_000_000,
                cached_input_tokens=380_000_000,
                total_output_tokens=15_000_000,
            )
            self._write_codex_session(
                home,
                session_id="child-codex-b",
                message="Trace the trust-fund inputs.",
                timestamp=now + timedelta(minutes=10),
                cwd="/tmp/crfb-tob-impacts",
                parent_session_id="root-codex",
                spawn_depth=2,
                total_input_tokens=350_000_000,
                cached_input_tokens=330_000_000,
                total_output_tokens=10_000_000,
            )
            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )
            for session_id in ("root-codex", "child-codex-a", "child-codex-b"):
                self._approve_public(db_path, shared, session_id)

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            with app.test_client() as client:
                analysis = client.get("/analysis")

            self.assertEqual(analysis.status_code, 200)
            self.assertIn(b"Context explosion", analysis.data)
            self.assertIn(b"mark sarneys email", analysis.data.lower())
            self.assertIn(b"mostly inherited context", analysis.data.lower())
            self.assertIn(b"2 child sessions", analysis.data)
            self.assertIn(b"spawn depth 2", analysis.data.lower())

    def test_legacy_analysis_stops_before_private_lineage_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_mixed_visibility_codex_lineage(root)
            app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)

            with app.test_client() as client:
                analysis = client.get("/analysis")

            self.assertEqual(analysis.status_code, 200)
            self.assertNotIn(b"private root sentinel", analysis.data.lower())
            self.assertNotIn(b"private-root", analysis.data.lower())

    @unittest.skipUnless(
        shutil.which("bun") and shutil.which("node"),
        "bun and node are required for the Next.js query regression",
    )
    def test_next_analysis_stops_before_private_lineage_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_mixed_visibility_codex_lineage(root)
            web_dir = Path(__file__).resolve().parents[1] / "web"
            with tempfile.TemporaryDirectory(
                prefix=".logpile-next-test-",
                dir=web_dir,
            ) as build_dir:
                bundle = Path(build_dir) / "db.mjs"
                subprocess.run(
                    [
                        "bun",
                        "build",
                        "./src/lib/db.ts",
                        "--target=node",
                        "--external=better-sqlite3",
                        f"--outfile={bundle}",
                    ],
                    cwd=web_dir,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                script = (
                    f"const db = await import({json.dumps(bundle.as_uri())});"
                    "process.stdout.write(JSON.stringify("
                    "db.getContextExplosionWorkstreams(6)));"
                )
                result = subprocess.run(
                    ["node", "-e", script],
                    cwd=web_dir,
                    env={
                        **os.environ,
                        "LOGPILE_DB_PATH": str(db_path),
                        "LOGPILE_SHARED_DIR": str(shared),
                        "LOGPILE_PUBLIC_MODE": "true",
                    },
                    check=True,
                    capture_output=True,
                    text=True,
                )

            self.assertEqual(json.loads(result.stdout), [])
            self.assertNotIn("PRIVATE ROOT SENTINEL", result.stdout)

    def test_sessions_and_api_can_filter_by_objective_family(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "alice"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._write_claude_session(
                home,
                session_id="match-1",
                message="Make more progress on Logpile analytics.",
            )
            self._write_claude_session(
                home,
                session_id="match-2",
                message="Make more progress on Logpile analytics.",
            )
            self._write_claude_session(
                home,
                session_id="other-1",
                message="Investigate deployment regressions in the docs site.",
            )
            self._prepare_user(db_path, username="alice")
            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            objective = normalize_objective_family("Make more progress on Logpile analytics.")
            self.assertIsNotNone(objective)

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)
            with app.test_client() as client:
                page = client.get(f"/sessions?objective={objective}&objectiveLabel=Logpile")
                payload = client.get(f"/api/sessions?objective={objective}").get_json()

            self.assertEqual(page.status_code, 200)
            self.assertIn(b"Objective family", page.data)
            self.assertIn(b"Logpile", page.data)
            self.assertIn(b"match-1", page.data)
            self.assertIn(b"match-2", page.data)
            self.assertNotIn(b"other-1", page.data)
            self.assertEqual(sorted(row["session_id"] for row in payload), ["match-1", "match-2"])

    def test_invalid_objective_filter_is_rejected_consistently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(root=root)
            app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)

            with app.test_client() as client:
                page = client.get("/sessions?objective=%40%40%40")
                payload = client.get("/api/sessions?objective=%40%40%40")

            self.assertEqual(page.status_code, 200)
            self.assertIn(b"Invalid objective filter", page.data)
            self.assertEqual(payload.status_code, 400)

    def test_api_user_profile_defaults_to_human_direct_lens(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared = root / "shared"
            db_path = root / "logpile.db"
            home = root / "alice"

            self._write_claude_session(
                home,
                session_id="direct-1",
                message="Ship the direct workflow.",
                cwd="/tmp/direct-app",
            )
            self._write_claude_session(
                home,
                session_id="pipeline-1",
                message="You are a senior statutory-fidelity reviewer for RAC...",
                cwd="/tmp/eval-app",
            )
            self._prepare_user(db_path, username="alice")
            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)
            with app.test_client() as client:
                default_profile = client.get("/api/users/alice").get_json()
                default_stats = client.get("/api/users/alice/stats").get_json()
                default_sessions = client.get("/api/users/alice/sessions").get_json()
                all_profile = client.get("/api/users/alice?origin=all").get_json()
                all_sessions = client.get("/api/users/alice/sessions?origin=all").get_json()

            self.assertEqual(default_profile["summary"]["total_sessions"], 1)
            self.assertEqual(default_stats["summary"]["total_sessions"], 1)
            self.assertEqual(default_sessions["total"], 1)
            self.assertEqual(all_profile["summary"]["total_sessions"], 2)
            self.assertEqual(all_sessions["total"], 2)

    def test_duplicate_display_names_stay_distinct_in_chart_apis(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(
                root=root,
                username="alice",
                session_id="session-a",
                message="alpha",
            )
            self._seed_user(
                root=root,
                username="bob",
                session_id="session-b",
                message="beta",
            )

            with open_sqlite(db_path) as conn:
                update_user(conn, "alice", display_name="Sam")
                update_user(conn, "bob", display_name="Sam")
                conn.commit()
            self._approve_public(db_path, shared, "session-a")
            self._approve_public(db_path, shared, "session-b")

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            with app.test_client() as client:
                messages_payload = client.get("/api/messages-per-day").get_json()
                error_payload = client.get("/api/error-rate").get_json()

            message_labels = [dataset["label"] for dataset in messages_payload["datasets"]]
            self.assertEqual(len(message_labels), 2)
            self.assertEqual(len(set(message_labels)), 2)
            self.assertTrue(all(label.startswith("Sam (@") for label in message_labels))

            self.assertEqual(len(error_payload["labels"]), 2)
            self.assertEqual(len(set(error_payload["labels"])), 2)
            self.assertTrue(all(label.startswith("Sam (@") for label in error_payload["labels"]))

    def test_messages_per_day_buckets_by_event_day_not_session_start(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "alice"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_user(db_path, username="alice")

            start = datetime.now(timezone.utc) - timedelta(days=3)
            later = datetime.now(timezone.utc) - timedelta(days=1)
            write_jsonl(
                home / ".claude" / "projects" / "-Users-alice-demo" / "long-session.jsonl",
                [
                    {
                        "timestamp": start.isoformat().replace("+00:00", "Z"),
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "kick off a multi-day task"},
                    },
                    {
                        "timestamp": later.isoformat().replace("+00:00", "Z"),
                        "type": "assistant",
                        "message": {
                            "id": "msg-late",
                            "model": "claude-3.7",
                            "usage": {"input_tokens": 1, "output_tokens": 2},
                            "content": [{"type": "text", "text": "done two days later"}],
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

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)
            with app.test_client() as client:
                payload = client.get("/api/messages-per-day").get_json()

            # One message on each event day — not both dumped on the start day.
            self.assertEqual(
                payload["labels"],
                [start.date().isoformat(), later.date().isoformat()],
            )
            self.assertEqual(payload["datasets"][0]["data"], [1, 1])

    def test_path_apis_and_filters_use_extracted_session_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(
                root=root,
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

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            with app.test_client() as client:
                projects = client.get("/api/projects").get_json()
                paths = client.get("/api/paths?project=demo").get_json()
                filtered_sessions = client.get("/api/sessions?path=src/app.py").get_json()
                user_sessions = client.get("/api/users/alice/sessions?path=tests/test_sync.py").get_json()

            self.assertEqual(projects[0]["project"], "demo")
            self.assertEqual(projects[0]["sessions"], 1)
            self.assertEqual(projects[0]["messages"], 2)
            self.assertEqual(projects[0]["tool_calls"], 2)
            self.assertNotIn("workspace_root", projects[0])
            self.assertEqual(
                [(row["display_path"], row["writes"], row["reads"], row["searches"]) for row in paths],
                [
                    ("src/app.py", 1, 0, 1),
                    ("tests/test_sync.py", 0, 0, 1),
                ],
            )
            self.assertEqual(len(filtered_sessions), 1)
            self.assertEqual(filtered_sessions[0]["session_id"], "session-1")
            self.assertNotIn("workspace_root", filtered_sessions[0])
            self.assertEqual(user_sessions["total"], 1)
            self.assertEqual(user_sessions["sessions"][0]["session_id"], "session-1")

    def test_repo_apis_and_filters_use_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, branch = self._init_git_repo(root)
            workspace = repo / "packages"
            workspace.mkdir(parents=True, exist_ok=True)
            shared, db_path = self._seed_user(
                root=root,
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

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            with app.test_client() as client:
                repos = client.get("/api/repos").get_json()
                projects = client.get(f"/api/projects?repo={repo.name}").get_json()
                paths = client.get(f"/api/paths?repo={repo.name}").get_json()
                sessions = client.get(f"/api/sessions?repo={repo.name}&branch={branch}").get_json()
                user_sessions = client.get(
                    f"/api/users/alice/sessions?repo={repo.name}&branch={branch}&path=packages/src/app.py"
                ).get_json()

            self.assertEqual(repos[0]["repo_name"], repo.name)
            self.assertEqual(repos[0]["sessions"], 1)
            self.assertEqual(repos[0]["messages"], 2)
            self.assertEqual(repos[0]["tool_calls"], 1)
            self.assertEqual(repos[0]["worktrees"], 1)
            self.assertEqual(repos[0]["branches"], 1)
            self.assertNotIn("repo_root", repos[0])

            self.assertEqual(projects[0]["repo_name"], repo.name)
            self.assertNotIn("repo_root", projects[0])
            self.assertNotIn("worktree_root", projects[0])

            self.assertEqual(paths[0]["repo_name"], repo.name)
            self.assertEqual(paths[0]["display_path"], "packages/src/app.py")
            self.assertEqual(paths[0]["repo_relative_path"], "packages/src/app.py")

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["repo_name"], repo.name)
            self.assertNotIn("repo_root", sessions[0])
            self.assertNotIn("git_branch", sessions[0])

            self.assertEqual(user_sessions["total"], 1)
            self.assertEqual(user_sessions["sessions"][0]["repo_name"], repo.name)
            self.assertNotIn("repo_root", user_sessions["sessions"][0])

    def test_activity_filters_and_metrics_are_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "alice"
            shared = root / "shared"
            db_path = root / "logpile.db"
            write_jsonl(
                home / ".claude" / "projects" / "-Users-alice-demo" / "session-1.jsonl",
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
                                {"type": "tool_use", "name": "Bash", "id": "build-1", "input": {"command": "npm run build"}},
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
                                {"type": "tool_result", "tool_use_id": "build-1", "is_error": False, "content": "built"},
                                {"type": "tool_result", "tool_use_id": "commit-1", "is_error": False, "content": "[main abc123] ship"},
                            ]
                        },
                    },
                ],
            )
            self._prepare_user(db_path, username="alice")
            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)
            with app.test_client() as client:
                test_failed = client.get("/api/sessions?activity=test_failed").get_json()
                writes = client.get("/api/users/alice/sessions?activity=write").get_json()
                commits = client.get("/api/users/alice/sessions?activity=git_commit").get_json()
                bad_filter = client.get("/api/sessions?activity=unknown")
                stats = client.get("/api/users/alice/stats").get_json()

            self.assertEqual(len(test_failed), 1)
            self.assertEqual(test_failed[0]["test_run_count"], 1)
            self.assertEqual(test_failed[0]["test_failure_count"], 1)
            self.assertEqual(test_failed[0]["build_run_count"], 1)
            self.assertEqual(test_failed[0]["git_commit_count"], 1)
            self.assertEqual(test_failed[0]["session_status"], "partial")
            self.assertIn("Made progress", test_failed[0]["session_outcome"])

            self.assertEqual(writes["total"], 1)
            self.assertEqual(writes["sessions"][0]["write_path_count"], 1)
            self.assertEqual(writes["sessions"][0]["session_goal"], "Ship it")
            self.assertIn("Touched 1 file", writes["sessions"][0]["session_summary"])
            self.assertEqual(commits["total"], 1)
            self.assertEqual(commits["sessions"][0]["git_commit_count"], 1)
            self.assertEqual(bad_filter.status_code, 400)

            self.assertEqual(stats["summary"]["test_runs"], 1)
            self.assertEqual(stats["summary"]["test_failures"], 1)
            self.assertEqual(stats["summary"]["build_runs"], 1)
            self.assertEqual(stats["summary"]["build_failures"], 0)
            self.assertEqual(stats["summary"]["git_commits"], 1)
            self.assertEqual(stats["summary"]["partial_sessions"], 1)
            self.assertEqual(stats["summary"]["success_sessions"], 0)

    def test_publish_queue_and_review_apis_are_private_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(root=root, message="Publish this safely.")

            private_app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)
            with private_app.test_client() as client:
                queue_payload = client.get("/api/publish/queue?visibility=all&reviews=1").get_json()
                review_payload = client.get("/api/publish/review/session-1").get_json()

            self.assertEqual(queue_payload["total"], 1)
            self.assertEqual(queue_payload["candidates"][0]["session_id"], "session-1")
            self.assertEqual(queue_payload["candidates"][0]["visibility"], "public")
            self.assertEqual(queue_payload["candidates"][0]["review_recommendation"], "public")
            self.assertEqual(review_payload["session_id"], "session-1")
            self.assertEqual(review_payload["recommendation"], "public")

            public_app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            with public_app.test_client() as client:
                self.assertEqual(client.get("/api/publish/queue").status_code, 404)
                self.assertEqual(client.get("/api/publish/review/session-1").status_code, 404)

    def test_unlisted_profiles_are_direct_only_in_public_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(root=root)

            with open_sqlite(db_path) as conn:
                update_user(conn, "alice", profile_visibility="unlisted")
                conn.commit()

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            with app.test_client() as client:
                self.assertNotIn(b"alice", client.get("/u").data.lower())
                self.assertNotIn(b"hello world", client.get("/").data)
                self.assertNotIn(b"hello world", client.get("/sessions").data)
                self.assertEqual(client.get("/api/sessions").get_json(), [])
                self.assertEqual(client.get("/analysis").status_code, 200)
                self.assertNotIn(b"alice", client.get("/analysis").data.lower())

                profile = client.get("/u/alice")
                profile_json = client.get("/api/users/alice").get_json()
                sessions_json = client.get("/api/users/alice/sessions").get_json()

                self.assertEqual(profile.status_code, 200)
                self.assertEqual(profile_json["user"]["profile_visibility"], "unlisted")
                self.assertEqual(profile_json["summary"]["total_sessions"], 1)
                self.assertEqual(sessions_json["total"], 1)

    def test_private_mode_is_more_permissive_than_public_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(root=root)

            with open_sqlite(db_path) as conn:
                update_user(conn, "alice", profile_visibility="unlisted")
                set_session_visibility(conn, "session-1", "unlisted", shared_dir=shared)
                conn.commit()

            public_app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            private_app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)

            with public_app.test_client() as client:
                self.assertEqual(client.get("/api/sessions").get_json(), [])
                self.assertEqual(
                    client.get("/api/users/alice/sessions").status_code,
                    404,
                )

            with private_app.test_client() as client:
                self.assertEqual(len(client.get("/api/sessions").get_json()), 1)
                self.assertEqual(client.get("/api/users/alice/sessions").get_json()["total"], 1)

    def test_private_sessions_return_404(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(root=root)

            with open_sqlite(db_path) as conn:
                set_session_visibility(conn, "session-1", "private", shared_dir=shared)
                conn.commit()

            public_app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            private_app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)

            with public_app.test_client() as client:
                self.assertEqual(client.get("/sessions/session-1").status_code, 404)
                self.assertEqual(client.get("/api/sessions").get_json(), [])
                self.assertEqual(client.get("/api/users/alice").status_code, 404)
                self.assertEqual(
                    client.get("/api/users/alice/sessions").status_code,
                    404,
                )

            with private_app.test_client() as client:
                self.assertEqual(client.get("/sessions/session-1").status_code, 404)
                self.assertEqual(client.get("/api/sessions").get_json(), [])
                self.assertEqual(client.get("/api/users/alice").get_json()["summary"]["total_sessions"], 0)
                self.assertEqual(client.get("/api/users/alice/sessions").get_json()["total"], 0)

    def test_private_mode_session_filter_lists_private_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(root=root)

            with open_sqlite(db_path) as conn:
                update_user(conn, "alice", profile_visibility="private")
                conn.commit()

            app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)
            with app.test_client() as client:
                page = client.get("/sessions")

            self.assertEqual(page.status_code, 200)
            self.assertIn(b'value="alice"', page.data)

    def test_public_mode_restricts_aggregate_json_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(root=root)

            with open_sqlite(db_path) as conn:
                update_user(conn, "alice", profile_visibility="unlisted")
                conn.execute(
                    "UPDATE sessions SET error_count = 2 WHERE session_id = 'session-1'"
                )
                conn.execute(
                    """
                    INSERT INTO tool_calls (session_id, tool_name, command, timestamp, is_error)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("session-1", "Bash", "rg TODO", "2026-04-10T10:00:02Z", 0),
                )
                conn.commit()

            public_app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            private_app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)

            with public_app.test_client() as client:
                self.assertEqual(client.get("/api/messages-per-day").get_json()["datasets"], [])
                self.assertEqual(client.get("/api/messages-per-tool").get_json()["datasets"], [])
                self.assertEqual(client.get("/api/top-tools").get_json()["labels"], [])
                self.assertEqual(client.get("/api/error-rate").get_json()["labels"], [])

            with private_app.test_client() as client:
                self.assertEqual(len(client.get("/api/messages-per-day").get_json()["datasets"]), 1)
                self.assertEqual(len(client.get("/api/messages-per-tool").get_json()["datasets"]), 1)
                self.assertEqual(client.get("/api/top-tools").get_json()["labels"], ["Bash"])
                self.assertEqual(len(client.get("/api/error-rate").get_json()["labels"]), 1)

    def test_user_rules_api_is_private_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared, db_path = self._seed_user(root=root)

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

            public_app = create_app(db_path=db_path, shared_dir=shared, public_mode=True)
            private_app = create_app(db_path=db_path, shared_dir=shared, public_mode=False)

            with public_app.test_client() as client:
                self.assertEqual(client.get("/api/users/alice/rules").status_code, 404)

            with private_app.test_client() as client:
                payload = client.get("/api/users/alice/rules").get_json()

            self.assertEqual(len(payload["rules"]), 1)
            self.assertEqual(payload["rules"][0]["pattern"], "demo")
            self.assertEqual(payload["rules"][0]["visibility"], "private")


if __name__ == "__main__":
    unittest.main()
