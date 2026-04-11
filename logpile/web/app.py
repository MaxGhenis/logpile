"""Flask web application for Logpile."""
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from ..db import get_user_by_identifier, init_db, list_visibility_rules
from ..parsers import render_claudecode_transcript, render_codex_transcript


def create_app(db_path: Path, shared_dir: Path | None = None, public_mode: bool = False) -> Flask:
    init_db(db_path)
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["SHARED_DIR"] = shared_dir
    app.config["PUBLIC_MODE"] = public_mode

    def _is_public_mode() -> bool:
        return bool(app.config["PUBLIC_MODE"])

    def _db():
        if "db" not in g:
            import sqlite3

            conn = sqlite3.connect(app.config["DB_PATH"])
            conn.row_factory = sqlite3.Row
            g.db = conn
        return g.db

    @app.teardown_appcontext
    def close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def _listed_profile_clause(alias: str = "u") -> str:
        if _is_public_mode():
            return f"{alias}.listed_public = 1"
        return f"{alias}.listed_private = 1"

    def _direct_profile_clause(alias: str = "u") -> str:
        if _is_public_mode():
            return f"{alias}.direct_public = 1"
        return f"{alias}.direct_private = 1"

    def _listed_session_clause(session_alias: str = "s", user_alias: str = "u") -> str:
        del user_alias
        if _is_public_mode():
            return f"{session_alias}.listed_public = 1"
        return f"{session_alias}.listed_private = 1"

    def _profile_session_clause(alias: str = "s") -> str:
        if _is_public_mode():
            return f"{alias}.direct_public = 1"
        return f"{alias}.direct_private = 1"

    def _profile_is_directly_visible(row) -> bool:
        if not row:
            return False
        if not _is_public_mode():
            return True
        if "direct_public" in row.keys():
            return bool(row["direct_public"])
        return row["profile_visibility"] in ("public", "unlisted")

    def _parse_int_arg(
        name: str,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        raw = request.args.get(name)
        if raw in (None, ""):
            value = default
        else:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                abort(400, description=f"Invalid integer for '{name}'")
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _user_label_map(rows) -> dict[str, str]:
        counts: defaultdict[str, int] = defaultdict(int)
        for row in rows:
            display = row["user_display_name"] or row["username"] or row["user_key"]
            counts[display] += 1

        labels: dict[str, str] = {}
        for row in rows:
            user_key = row["user_key"]
            display = row["user_display_name"] or row["username"] or user_key
            labels[user_key] = f"{display} (@{user_key})" if counts[display] > 1 else display
        return labels

    def _fmt_ts(ts: str | None) -> str:
        if not ts:
            return "—"
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ts[:16]

    def _fmt_duration(secs: float | None) -> str:
        if secs is None:
            return "—"
        secs = int(secs)
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"

    def _display_project(project: str | None) -> str:
        if not project or project == "unknown":
            return "unknown"
        try:
            leaf = Path(project).name
        except (TypeError, ValueError):
            return str(project)
        return leaf or str(project)

    def _serialize_user(row) -> dict:
        return {
            "slug": row["slug"],
            "username": row["username"],
            "display_name": row["display_name"] or row["username"],
            "bio": row["bio"],
            "avatar_url": row["avatar_url"],
            "profile_visibility": row["profile_visibility"],
            "default_session_visibility": row["default_session_visibility"],
        }

    def _serialize_session(row) -> dict:
        item = dict(row)
        if _is_public_mode():
            for key in (
                "workspace_root",
                "worktree_root",
                "repo_root",
                "git_branch",
                "git_commit",
                "git_dirty",
            ):
                item.pop(key, None)
        return item

    def _get_user_catalog(identifier: str):
        db = _db()
        return db.execute(
            """
            SELECT *
            FROM user_catalog
            WHERE slug = ? OR username = ?
            LIMIT 1
            """,
            (identifier, identifier),
        ).fetchone()

    def _activity_clause(name: str | None, alias: str = "s") -> str | None:
        if not name:
            return None
        normalized = name.strip().lower()
        clauses = {
            "write": f"COALESCE({alias}.write_path_count, 0) > 0",
            "read": f"COALESCE({alias}.read_path_count, 0) > 0",
            "search": f"COALESCE({alias}.search_path_count, 0) > 0",
            "test": f"COALESCE({alias}.test_run_count, 0) > 0",
            "test_failed": f"COALESCE({alias}.test_failure_count, 0) > 0",
            "lint": f"COALESCE({alias}.lint_run_count, 0) > 0",
            "lint_failed": f"COALESCE({alias}.lint_failure_count, 0) > 0",
            "build": f"COALESCE({alias}.build_run_count, 0) > 0",
            "build_failed": f"COALESCE({alias}.build_failure_count, 0) > 0",
            "format": f"COALESCE({alias}.format_run_count, 0) > 0",
            "format_failed": f"COALESCE({alias}.format_failure_count, 0) > 0",
            "git_status": f"COALESCE({alias}.git_status_count, 0) > 0",
            "git_diff": f"COALESCE({alias}.git_diff_count, 0) > 0",
            "git_commit": f"COALESCE({alias}.git_commit_count, 0) > 0",
            "error": f"COALESCE({alias}.error_count, 0) > 0",
        }
        if normalized not in clauses:
            abort(400, description=f"Unsupported activity filter: {name}")
        return clauses[normalized]

    def _build_user_profile(identifier: str) -> dict | None:
        db = _db()
        user = _get_user_catalog(identifier)
        if not _profile_is_directly_visible(user):
            return None

        summary = db.execute(
            f"""
            SELECT
                COUNT(*) AS total_sessions,
                SUM(user_message_count + assistant_message_count) AS total_messages,
                SUM(tool_call_count) AS total_tool_calls,
                SUM(total_input_tokens + total_output_tokens) AS total_tokens,
                COUNT(DISTINCT substr(first_timestamp, 1, 10)) AS active_days,
                COUNT(DISTINCT CASE
                    WHEN project IS NOT NULL AND project != '' AND project != 'unknown'
                    THEN project
                END) AS known_projects,
                COUNT(DISTINCT CASE
                    WHEN repo_name IS NOT NULL AND repo_name != ''
                    THEN repo_name
                END) AS known_repos,
                SUM(COALESCE(write_path_count, 0)) AS write_paths,
                SUM(COALESCE(test_run_count, 0)) AS test_runs,
                SUM(COALESCE(test_failure_count, 0)) AS test_failures,
                SUM(COALESCE(build_run_count, 0)) AS build_runs,
                SUM(COALESCE(build_failure_count, 0)) AS build_failures,
                SUM(COALESCE(git_commit_count, 0)) AS git_commits,
                SUM(CASE WHEN COALESCE(session_status, 'exploration') = 'success' THEN 1 ELSE 0 END) AS success_sessions,
                SUM(CASE WHEN COALESCE(session_status, 'exploration') = 'partial' THEN 1 ELSE 0 END) AS partial_sessions,
                SUM(CASE WHEN COALESCE(session_status, 'exploration') = 'failed' THEN 1 ELSE 0 END) AS failed_sessions,
                SUM(CASE WHEN COALESCE(session_status, 'exploration') = 'exploration' THEN 1 ELSE 0 END) AS exploration_sessions,
                MIN(first_timestamp) AS first_seen,
                MAX(last_timestamp) AS last_seen
            FROM session_catalog s
            WHERE { _profile_session_clause("s") } AND s.user_slug = ?
            """,
            (user["slug"],),
        ).fetchone()

        cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        activity_rows = db.execute(
            f"""
            SELECT
                substr(first_timestamp, 1, 10) AS day,
                COUNT(*) AS sessions,
                SUM(user_message_count + assistant_message_count) AS messages,
                SUM(tool_call_count) AS tool_calls
            FROM session_catalog s
            WHERE { _profile_session_clause("s") }
              AND s.user_slug = ?
              AND s.first_timestamp >= ?
            GROUP BY day
            ORDER BY day
            """,
            (user["slug"], cutoff),
        ).fetchall()

        source_rows = db.execute(
            f"""
            SELECT
                source,
                COUNT(*) AS sessions,
                SUM(user_message_count + assistant_message_count) AS messages,
                SUM(tool_call_count) AS tool_calls
            FROM session_catalog s
            WHERE { _profile_session_clause("s") } AND s.user_slug = ?
            GROUP BY source
            ORDER BY sessions DESC, messages DESC
            """,
            (user["slug"],),
        ).fetchall()

        tool_rows = db.execute(
            f"""
            SELECT tc.tool_name, COUNT(*) AS cnt
            FROM tool_calls tc
            JOIN session_catalog s ON s.session_id = tc.session_id
            WHERE { _profile_session_clause("s") } AND s.user_slug = ?
            GROUP BY tc.tool_name
            ORDER BY cnt DESC
            LIMIT 12
            """,
            (user["slug"],),
        ).fetchall()

        model_rows = db.execute(
            f"""
            SELECT
                CASE
                    WHEN model IS NULL OR model = '' THEN 'unknown'
                    ELSE model
                END AS model_name,
                COUNT(*) AS sessions,
                SUM(user_message_count + assistant_message_count) AS messages
            FROM session_catalog s
            WHERE { _profile_session_clause("s") } AND s.user_slug = ?
            GROUP BY model_name
            ORDER BY sessions DESC, messages DESC
            LIMIT 8
            """,
            (user["slug"],),
        ).fetchall()

        recent_rows = db.execute(
            f"""
            SELECT
                s.session_id,
                s.source,
                s.project,
                s.repo_name,
                s.model,
                s.visibility,
                s.session_status,
                s.session_summary,
                s.session_outcome,
                s.first_timestamp,
                s.duration_seconds,
                s.user_message_count,
                s.assistant_message_count,
                s.tool_call_count
            FROM session_catalog s
            WHERE { _profile_session_clause("s") } AND s.user_slug = ?
            ORDER BY s.first_timestamp DESC
            LIMIT 12
            """,
            (user["slug"],),
        ).fetchall()

        activity = {
            "labels": [r["day"] for r in activity_rows],
            "messages": [r["messages"] or 0 for r in activity_rows],
            "sessions": [r["sessions"] or 0 for r in activity_rows],
            "tool_calls": [r["tool_calls"] or 0 for r in activity_rows],
        }
        source_breakdown = {
            "labels": [
                "Claude Code" if r["source"] == "claudecode"
                else "Codex" if r["source"] == "codex"
                else r["source"]
                for r in source_rows
            ],
            "sessions": [r["sessions"] or 0 for r in source_rows],
            "messages": [r["messages"] or 0 for r in source_rows],
            "colors": [
                "#f4a261" if r["source"] == "claudecode" else "#4f86f7"
                for r in source_rows
            ],
        }

        return {
            "user": user,
            "summary": summary,
            "activity": activity,
            "source_rows": source_rows,
            "source_breakdown": source_breakdown,
            "tool_rows": tool_rows,
            "model_rows": model_rows,
            "recent_rows": recent_rows,
        }

    app.jinja_env.globals["fmt_ts"] = _fmt_ts
    app.jinja_env.globals["fmt_dur"] = _fmt_duration
    app.jinja_env.globals["display_project"] = _display_project
    app.jinja_env.globals["public_mode"] = public_mode

    @app.route("/")
    def dashboard():
        db = _db()
        stats = db.execute(
            f"""
            SELECT
                COUNT(*) AS total_sessions,
                SUM(user_message_count) AS total_user_msgs,
                SUM(assistant_message_count) AS total_assistant_msgs,
                SUM(tool_call_count) AS total_tool_calls,
                SUM(total_input_tokens) AS total_input_tokens,
                SUM(total_output_tokens) AS total_output_tokens,
                COUNT(DISTINCT user_slug) AS active_users,
                COUNT(DISTINCT project) AS total_projects
            FROM session_catalog s
            LEFT JOIN user_catalog u ON u.slug = s.user_slug
            WHERE { _listed_session_clause("s", "u") }
            """
        ).fetchone()

        recent = db.execute(
            f"""
            SELECT
                s.session_id,
                s.source,
                s.username,
                s.user_slug,
                COALESCE(u.display_name, s.username) AS user_display_name,
                s.project,
                s.first_timestamp,
                s.user_message_count,
                s.assistant_message_count,
                s.total_input_tokens + s.total_output_tokens AS tokens,
                s.first_user_message
            FROM session_catalog s
            LEFT JOIN user_catalog u ON u.slug = s.user_slug
            WHERE { _listed_session_clause("s", "u") }
            ORDER BY s.first_timestamp DESC
            LIMIT 10
            """
        ).fetchall()
        return render_template("dashboard.html", stats=stats, recent=recent)

    @app.route("/api/messages-per-day")
    def api_messages_per_day():
        db = _db()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        rows = db.execute(
            f"""
            SELECT
                substr(s.first_timestamp, 1, 10) AS day,
                COALESCE(s.user_slug, s.username) AS user_key,
                s.username,
                COALESCE(u.display_name, s.username) AS user_display_name,
                SUM(s.user_message_count + s.assistant_message_count) AS msgs
            FROM session_catalog s
            LEFT JOIN user_catalog u ON u.slug = s.user_slug
            WHERE { _listed_session_clause("s", "u") } AND s.first_timestamp >= ?
            GROUP BY day, user_key, s.username, user_display_name
            ORDER BY day
            """,
            (cutoff,),
        ).fetchall()

        days_set = sorted({r["day"] for r in rows})
        user_labels = _user_label_map(rows)
        users = sorted(user_labels, key=lambda user_key: user_labels[user_key].lower())
        pivot = defaultdict(lambda: defaultdict(int))
        for row in rows:
            pivot[row["day"]][row["user_key"]] = row["msgs"]

        colors = [
            "#4f86f7",
            "#f7864f",
            "#4ff786",
            "#f74f86",
            "#f7f44f",
            "#864ff7",
            "#4ff7f4",
            "#f74ff4",
        ]
        datasets = []
        for index, user in enumerate(users):
            datasets.append(
                {
                    "label": user_labels[user],
                    "data": [pivot[day][user] for day in days_set],
                    "borderColor": colors[index % len(colors)],
                    "backgroundColor": colors[index % len(colors)] + "33",
                    "tension": 0.3,
                    "fill": False,
                }
            )

        return jsonify({"labels": days_set, "datasets": datasets})

    @app.route("/api/messages-per-tool")
    def api_messages_per_tool():
        db = _db()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        rows = db.execute(
            f"""
            SELECT
                substr(first_timestamp, 1, 10) AS day,
                source,
                SUM(user_message_count + assistant_message_count) AS msgs
            FROM session_catalog s
            LEFT JOIN user_catalog u ON u.slug = s.user_slug
            WHERE { _listed_session_clause("s", "u") } AND first_timestamp >= ?
            GROUP BY day, source
            ORDER BY day
            """,
            (cutoff,),
        ).fetchall()

        days_set = sorted({r["day"] for r in rows})
        pivot = defaultdict(lambda: defaultdict(int))
        for row in rows:
            pivot[row["day"]][row["source"]] = row["msgs"]

        source_colors = {
            "claudecode": {"border": "#f4a261", "bg": "#f4a26133"},
            "codex": {"border": "#4f86f7", "bg": "#4f86f733"},
        }
        sources = sorted({r["source"] for r in rows})
        datasets = []
        for src in sources:
            color = source_colors.get(src, {"border": "#aaaaaa", "bg": "#aaaaaa33"})
            datasets.append(
                {
                    "label": "CC" if src == "claudecode" else "Codex" if src == "codex" else src,
                    "data": [pivot[day][src] for day in days_set],
                    "borderColor": color["border"],
                    "backgroundColor": color["bg"],
                    "tension": 0.3,
                    "fill": True,
                }
            )

        return jsonify({"labels": days_set, "datasets": datasets})

    @app.route("/api/top-tools")
    def api_top_tools():
        db = _db()
        rows = db.execute(
            f"""
            SELECT tc.tool_name, COUNT(*) AS cnt
            FROM tool_calls tc
            JOIN session_catalog s ON s.session_id = tc.session_id
            LEFT JOIN user_catalog u ON u.slug = s.user_slug
            WHERE { _listed_session_clause("s", "u") }
            GROUP BY tc.tool_name
            ORDER BY cnt DESC
            LIMIT 20
            """
        ).fetchall()
        return jsonify(
            {
                "labels": [r["tool_name"] for r in rows],
                "datasets": [
                    {
                        "label": "Calls",
                        "data": [r["cnt"] for r in rows],
                        "backgroundColor": "#4f86f7aa",
                        "borderColor": "#4f86f7",
                    }
                ],
            }
        )

    @app.route("/api/error-rate")
    def api_error_rate():
        db = _db()
        rows = db.execute(
            f"""
            SELECT
                COALESCE(s.user_slug, s.username) AS user_key,
                s.username,
                COALESCE(u.display_name, s.username) AS user_display_name,
                SUM(s.error_count) AS errors
            FROM session_catalog s
            LEFT JOIN user_catalog u ON u.slug = s.user_slug
            WHERE { _listed_session_clause("s", "u") }
            GROUP BY user_key, s.username, user_display_name
            ORDER BY errors DESC
            LIMIT 15
            """
        ).fetchall()
        user_labels = _user_label_map(rows)
        return jsonify(
            {
                "labels": [user_labels[r["user_key"]] for r in rows],
                "datasets": [
                    {
                        "label": "Errors",
                        "data": [r["errors"] or 0 for r in rows],
                        "backgroundColor": "#e76f5199",
                        "borderColor": "#e76f51",
                    }
                ],
            }
        )

    @app.route("/u")
    def people():
        db = _db()
        rows = db.execute(
            f"""
            SELECT
                u.slug,
                u.username,
                COALESCE(u.display_name, u.username) AS display_name,
                u.bio,
                COUNT(s.session_id) AS sessions,
                SUM(s.user_message_count + s.assistant_message_count) AS messages,
                SUM(s.tool_call_count) AS tool_calls,
                SUM(s.total_input_tokens + s.total_output_tokens) AS tokens,
                MIN(s.first_timestamp) AS first_seen,
                MAX(s.last_timestamp) AS last_seen
            FROM user_catalog u
            LEFT JOIN session_catalog s ON s.user_slug = u.slug AND { _listed_session_clause("s", "u") }
            WHERE { _listed_profile_clause("u") }
            GROUP BY u.slug, u.username, u.display_name, u.bio
            ORDER BY last_seen DESC, sessions DESC, u.slug
            """
        ).fetchall()
        return render_template("users.html", rows=rows)

    @app.route("/u/<slug>")
    def user_profile(slug):
        profile = _build_user_profile(slug)
        if not profile:
            abort(404)
        return render_template("user_profile.html", **profile)

    @app.route("/sessions")
    def sessions():
        db = _db()
        q = request.args.get("q", "").strip()
        source = request.args.get("source", "")
        user = request.args.get("user", "").strip()
        project = request.args.get("project", "").strip()
        repo = request.args.get("repo", "").strip()
        branch = request.args.get("branch", "").strip()
        activity = request.args.get("activity", "").strip()
        status_filter = request.args.get("status", "").strip()
        path_query = request.args.get("path", "").strip()
        page = _parse_int_arg("page", 1, minimum=1)
        per_page = 50
        invalid_activity = False

        clauses = [_listed_session_clause("s")]
        params: list = []

        if q:
            clauses.append("s.first_user_message LIKE ?")
            params.append(f"%{q}%")
        if source:
            clauses.append("s.source = ?")
            params.append(source)
        if user:
            user_row = get_user_by_identifier(db, user)
            if user_row:
                clauses.append("s.user_slug = ?")
                params.append(user_row["slug"])
            else:
                clauses.append("1 = 0")
        if project:
            clauses.append("s.project = ?")
            params.append(project)
        if repo:
            clauses.append("COALESCE(s.repo_name, '') = ?")
            params.append(repo)
        if branch:
            clauses.append("COALESCE(s.git_branch, '') = ?")
            params.append(branch)
        if status_filter:
            clauses.append("COALESCE(s.session_status, 'exploration') = ?")
            params.append(status_filter)
        if activity:
            try:
                clauses.append(_activity_clause(activity, "s"))
            except HTTPException as exc:
                if exc.code == 400:
                    invalid_activity = True
                else:
                    raise
        if path_query:
            clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM session_paths sp
                    WHERE sp.session_id = s.session_id
                      AND (
                          sp.display_path LIKE ?
                          OR COALESCE(sp.relative_path, '') LIKE ?
                          OR COALESCE(sp.repo_relative_path, '') LIKE ?
                      )
                )
                """
            )
            params.extend([f"%{path_query}%", f"%{path_query}%", f"%{path_query}%"])

        where = " AND ".join(clauses)
        if invalid_activity:
            total = 0
            rows = []
        else:
            total = db.execute(
                f"""
                SELECT COUNT(*)
                FROM session_catalog s
                LEFT JOIN user_catalog u ON u.slug = s.user_slug
                WHERE {where}
                """,
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"""
                SELECT
                    s.session_id,
                    s.source,
                    s.username,
                    s.user_slug,
                    COALESCE(u.display_name, s.username) AS user_display_name,
                    s.project,
                    s.repo_name,
                    s.session_goal,
                    s.session_summary,
                    s.session_outcome,
                    s.session_status,
                    s.visibility,
                    s.first_timestamp,
                    s.last_timestamp,
                    s.duration_seconds,
                    s.user_message_count,
                    s.assistant_message_count,
                    s.tool_call_count,
                    s.error_count,
                    s.write_path_count,
                    s.read_path_count,
                    s.search_path_count,
                    s.test_run_count,
                    s.test_failure_count,
                    s.lint_run_count,
                    s.lint_failure_count,
                    s.build_run_count,
                    s.build_failure_count,
                    s.format_run_count,
                    s.format_failure_count,
                    s.git_status_count,
                    s.git_diff_count,
                    s.git_commit_count,
                    s.total_input_tokens + s.total_output_tokens AS tokens,
                    s.first_user_message,
                    s.model
                FROM session_catalog s
                LEFT JOIN user_catalog u ON u.slug = s.user_slug
                WHERE {where}
                ORDER BY s.first_timestamp DESC
                LIMIT ? OFFSET ?
                """,
                params + [per_page, (page - 1) * per_page],
            ).fetchall()

        users = db.execute(
            f"""
            SELECT
                u.slug,
                COALESCE(u.display_name, u.username) AS display_name
            FROM user_catalog u
            WHERE { _listed_profile_clause("u") }
              AND EXISTS (
                  SELECT 1 FROM session_catalog s
                  WHERE s.user_slug = u.slug AND { _listed_session_clause("s") }
              )
            ORDER BY display_name, u.slug
            """
        ).fetchall()

        return render_template(
            "sessions.html",
            rows=rows,
            total=total,
            page=page,
            per_page=per_page,
            q=q,
            source=source,
            username=user,
            project=project,
            repo=repo,
            branch=branch,
            activity=activity,
            invalid_activity=invalid_activity,
            path_query=path_query,
            users=users,
        )

    @app.route("/sessions/<session_id>")
    def session_detail(session_id):
        db = _db()
        row = db.execute(
            """
            SELECT
                s.*,
                COALESCE(u.display_name, s.username) AS user_display_name,
                u.direct_public AS user_direct_public,
                u.direct_private AS user_direct_private
            FROM session_catalog s
            LEFT JOIN user_catalog u ON u.slug = s.user_slug
            WHERE s.session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if not row:
            abort(404)
        if not row["direct_private"]:
            abort(404)
        if _is_public_mode() and not row["user_direct_public"]:
            abort(404)

        candidate_paths = []
        if row["shared_path"]:
            candidate_paths.append(Path(row["shared_path"]))
            shared_root = app.config.get("SHARED_DIR")
            if shared_root:
                candidate_paths.append(
                    Path(shared_root)
                    / row["username"]
                    / row["source"]
                    / row["project"]
                    / Path(row["shared_path"]).name
                )
        if row["source_path"]:
            candidate_paths.append(Path(row["source_path"]))

        path = next((candidate for candidate in candidate_paths if candidate.exists()), None)

        turns = []
        if path:
            try:
                if row["source"] == "claudecode":
                    turns = render_claudecode_transcript(path)
                else:
                    turns = render_codex_transcript(path)
            except Exception as exc:
                turns = [{"type": "error", "content": f"Failed to parse transcript: {exc}"}]

        tool_calls = db.execute(
            "SELECT tool_name, command, is_error FROM tool_calls WHERE session_id = ?",
            (session_id,),
        ).fetchall()

        return render_template("session_detail.html", session=row, turns=turns, tool_calls=tool_calls)

    @app.route("/analysis")
    def analysis():
        db = _db()
        bash_rows = db.execute(
            f"""
            SELECT tc.command, COUNT(*) AS cnt
            FROM tool_calls tc
            JOIN session_catalog s ON s.session_id = tc.session_id
            LEFT JOIN user_catalog u ON u.slug = s.user_slug
            WHERE { _listed_session_clause("s", "u") }
              AND tc.tool_name IN ('Bash', 'shell', 'bash')
              AND tc.command IS NOT NULL
              AND tc.command != ''
            GROUP BY tc.command
            ORDER BY cnt DESC
            LIMIT 30
            """
        ).fetchall()

        bash_cmds = []
        for row in bash_rows:
            cmd = row["command"].strip()
            bash_cmds.append({"cmd": re.sub(r"\s+", " ", cmd)[:80], "cnt": row["cnt"]})

        tool_rows = db.execute(
            f"""
            SELECT tc.tool_name, COUNT(*) AS cnt
            FROM tool_calls tc
            JOIN session_catalog s ON s.session_id = tc.session_id
            LEFT JOIN user_catalog u ON u.slug = s.user_slug
            WHERE { _listed_session_clause("s", "u") }
            GROUP BY tc.tool_name
            ORDER BY cnt DESC
            LIMIT 25
            """
        ).fetchall()

        user_rows = db.execute(
            f"""
            SELECT
                s.user_slug AS slug,
                COALESCE(u.display_name, s.username) AS display_name,
                s.username,
                COUNT(*) AS sessions,
                SUM(s.user_message_count) AS user_msgs,
                SUM(s.tool_call_count) AS tool_calls,
                SUM(s.error_count) AS errors,
                SUM(s.total_input_tokens + s.total_output_tokens) AS tokens,
                MIN(s.first_timestamp) AS first_seen,
                MAX(s.last_timestamp) AS last_seen
            FROM session_catalog s
            LEFT JOIN user_catalog u ON u.slug = s.user_slug
            WHERE { _listed_session_clause("s", "u") }
            GROUP BY s.user_slug, display_name, s.username
            ORDER BY sessions DESC
            """
        ).fetchall()

        shared_rows = db.execute(
            f"""
            SELECT tc.command, COUNT(DISTINCT s.user_slug) AS users, COUNT(*) AS total
            FROM tool_calls tc
            JOIN session_catalog s ON s.session_id = tc.session_id
            LEFT JOIN user_catalog u ON u.slug = s.user_slug
            WHERE { _listed_session_clause("s", "u") }
              AND tc.command IS NOT NULL
              AND tc.command != ''
            GROUP BY tc.command
            HAVING users >= 2
            ORDER BY users DESC, total DESC
            LIMIT 20
            """
        ).fetchall()

        return render_template(
            "analysis.html",
            bash_cmds=bash_cmds,
            tool_rows=tool_rows,
            user_rows=user_rows,
            shared_rows=shared_rows,
        )

    @app.route("/api/sessions")
    def api_sessions():
        db = _db()
        source = request.args.get("source", "").strip()
        project = request.args.get("project", "").strip()
        repo = request.args.get("repo", "").strip()
        branch = request.args.get("branch", "").strip()
        activity = request.args.get("activity", "").strip()
        status_filter = request.args.get("status", "").strip()
        user = request.args.get("user", "").strip()
        path_query = request.args.get("path", "").strip()
        limit = _parse_int_arg("limit", 500, minimum=1, maximum=1000)
        clauses = [_listed_session_clause("s", "u")]
        params: list = []
        if source:
            clauses.append("s.source = ?")
            params.append(source)
        if project:
            clauses.append("s.project = ?")
            params.append(project)
        if repo:
            clauses.append("COALESCE(s.repo_name, '') = ?")
            params.append(repo)
        if branch:
            clauses.append("COALESCE(s.git_branch, '') = ?")
            params.append(branch)
        if activity:
            clauses.append(_activity_clause(activity, "s"))
        if status_filter:
            clauses.append("COALESCE(s.session_status, 'exploration') = ?")
            params.append(status_filter)
        if user:
            user_row = get_user_by_identifier(db, user)
            if user_row:
                clauses.append("s.user_slug = ?")
                params.append(user_row["slug"])
            else:
                clauses.append("1 = 0")
        if path_query:
            clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM session_paths sp
                    WHERE sp.session_id = s.session_id
                      AND (
                          sp.display_path LIKE ?
                          OR COALESCE(sp.relative_path, '') LIKE ?
                          OR COALESCE(sp.repo_relative_path, '') LIKE ?
                      )
                )
                """
            )
            params.extend([f"%{path_query}%", f"%{path_query}%", f"%{path_query}%"])
        rows = db.execute(
            f"""
            SELECT
                s.session_id,
                s.source,
                s.username,
                s.user_slug,
                COALESCE(u.display_name, s.username) AS user_display_name,
                s.project,
                s.repo_name,
                s.session_goal,
                s.session_summary,
                s.session_outcome,
                s.session_status,
                s.workspace_root,
                s.worktree_root,
                s.repo_root,
                s.git_branch,
                s.git_commit,
                s.git_dirty,
                s.visibility,
                s.first_timestamp,
                s.user_message_count,
                s.assistant_message_count,
                s.tool_call_count,
                s.error_count,
                s.write_path_count,
                s.read_path_count,
                s.search_path_count,
                s.test_run_count,
                s.test_failure_count,
                s.lint_run_count,
                s.lint_failure_count,
                s.build_run_count,
                s.build_failure_count,
                s.format_run_count,
                s.format_failure_count,
                s.git_status_count,
                s.git_diff_count,
                s.git_commit_count,
                s.total_input_tokens,
                s.total_output_tokens
            FROM session_catalog s
            LEFT JOIN user_catalog u ON u.slug = s.user_slug
            WHERE {' AND '.join(clauses)}
            ORDER BY s.first_timestamp DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
        return jsonify([_serialize_session(row) for row in rows])

    @app.route("/api/projects")
    def api_projects():
        db = _db()
        user = request.args.get("user", "").strip()
        repo = request.args.get("repo", "").strip()
        branch = request.args.get("branch", "").strip()
        limit = _parse_int_arg("limit", 100, minimum=1, maximum=500)
        clauses = [_listed_session_clause("s", "u")]
        params: list = []
        if user:
            user_row = get_user_by_identifier(db, user)
            if user_row:
                clauses.append("s.user_slug = ?")
                params.append(user_row["slug"])
            else:
                clauses.append("1 = 0")
        if repo:
            clauses.append("COALESCE(s.repo_name, '') = ?")
            params.append(repo)
        if branch:
            clauses.append("COALESCE(s.git_branch, '') = ?")
            params.append(branch)

        rows = db.execute(
            f"""
            WITH filtered_sessions AS (
                SELECT
                    s.session_id,
                    s.project,
                    s.repo_name,
                    s.workspace_root,
                    s.worktree_root,
                    s.repo_root,
                    s.user_message_count,
                    s.assistant_message_count,
                    s.tool_call_count
                FROM session_catalog s
                LEFT JOIN user_catalog u ON u.slug = s.user_slug
                WHERE {' AND '.join(clauses)}
            ),
            path_counts AS (
                SELECT
                    fs.project,
                    fs.repo_name,
                    fs.workspace_root,
                    fs.worktree_root,
                    fs.repo_root,
                    COUNT(DISTINCT COALESCE(sp.repo_relative_path, sp.relative_path, sp.display_path)) AS unique_paths
                FROM filtered_sessions fs
                LEFT JOIN session_paths sp ON sp.session_id = fs.session_id
                GROUP BY fs.project, fs.repo_name, fs.workspace_root, fs.worktree_root, fs.repo_root
            )
            SELECT
                fs.project,
                fs.repo_name,
                fs.workspace_root,
                fs.worktree_root,
                fs.repo_root,
                COUNT(*) AS sessions,
                SUM(fs.user_message_count + fs.assistant_message_count) AS messages,
                SUM(fs.tool_call_count) AS tool_calls,
                COALESCE(MAX(pc.unique_paths), 0) AS unique_paths
            FROM filtered_sessions fs
            LEFT JOIN path_counts pc
              ON COALESCE(fs.project, '') = COALESCE(pc.project, '')
             AND COALESCE(fs.repo_name, '') = COALESCE(pc.repo_name, '')
             AND COALESCE(fs.workspace_root, '') = COALESCE(pc.workspace_root, '')
             AND COALESCE(fs.worktree_root, '') = COALESCE(pc.worktree_root, '')
             AND COALESCE(fs.repo_root, '') = COALESCE(pc.repo_root, '')
            GROUP BY fs.project, fs.repo_name, fs.workspace_root, fs.worktree_root, fs.repo_root
            ORDER BY sessions DESC, messages DESC, fs.project, fs.repo_name
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
        payload = []
        for row in rows:
            item = dict(row)
            if _is_public_mode():
                item.pop("workspace_root", None)
                item.pop("worktree_root", None)
                item.pop("repo_root", None)
            payload.append(item)
        return jsonify(payload)

    @app.route("/api/repos")
    def api_repos():
        db = _db()
        user = request.args.get("user", "").strip()
        limit = _parse_int_arg("limit", 100, minimum=1, maximum=500)
        clauses = [_listed_session_clause("s", "u"), "s.repo_name IS NOT NULL", "s.repo_name != ''"]
        params: list = []
        if user:
            user_row = get_user_by_identifier(db, user)
            if user_row:
                clauses.append("s.user_slug = ?")
                params.append(user_row["slug"])
            else:
                clauses.append("1 = 0")

        rows = db.execute(
            f"""
            WITH filtered_sessions AS (
                SELECT
                    s.session_id,
                    s.repo_name,
                    s.repo_root,
                    s.worktree_root,
                    s.workspace_root,
                    s.git_branch,
                    s.user_message_count,
                    s.assistant_message_count,
                    s.tool_call_count,
                    s.last_timestamp
                FROM session_catalog s
                LEFT JOIN user_catalog u ON u.slug = s.user_slug
                WHERE {' AND '.join(clauses)}
            ),
            path_counts AS (
                SELECT
                    fs.repo_name,
                    fs.repo_root,
                    COUNT(DISTINCT COALESCE(sp.repo_relative_path, sp.relative_path, sp.display_path)) AS unique_paths
                FROM filtered_sessions fs
                LEFT JOIN session_paths sp ON sp.session_id = fs.session_id
                GROUP BY fs.repo_name, fs.repo_root
            )
            SELECT
                fs.repo_name,
                fs.repo_root,
                COUNT(*) AS sessions,
                COUNT(DISTINCT COALESCE(fs.worktree_root, fs.workspace_root)) AS worktrees,
                COUNT(DISTINCT CASE
                    WHEN fs.git_branch IS NOT NULL AND fs.git_branch != ''
                    THEN fs.git_branch
                END) AS branches,
                SUM(fs.user_message_count + fs.assistant_message_count) AS messages,
                SUM(fs.tool_call_count) AS tool_calls,
                COALESCE(MAX(pc.unique_paths), 0) AS unique_paths,
                MAX(fs.last_timestamp) AS last_seen
            FROM filtered_sessions fs
            LEFT JOIN path_counts pc
              ON COALESCE(fs.repo_name, '') = COALESCE(pc.repo_name, '')
             AND COALESCE(fs.repo_root, '') = COALESCE(pc.repo_root, '')
            GROUP BY fs.repo_name, fs.repo_root
            ORDER BY sessions DESC, messages DESC, fs.repo_name
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
        payload = []
        for row in rows:
            item = dict(row)
            if _is_public_mode():
                item.pop("repo_root", None)
            payload.append(item)
        return jsonify(payload)

    @app.route("/api/paths")
    def api_paths():
        db = _db()
        project = request.args.get("project", "").strip()
        repo = request.args.get("repo", "").strip()
        branch = request.args.get("branch", "").strip()
        user = request.args.get("user", "").strip()
        limit = _parse_int_arg("limit", 100, minimum=1, maximum=500)
        clauses = [_listed_session_clause("s", "u")]
        params: list = []
        if project:
            clauses.append("s.project = ?")
            params.append(project)
        if repo:
            clauses.append("COALESCE(s.repo_name, '') = ?")
            params.append(repo)
        if branch:
            clauses.append("COALESCE(s.git_branch, '') = ?")
            params.append(branch)
        if user:
            user_row = get_user_by_identifier(db, user)
            if user_row:
                clauses.append("s.user_slug = ?")
                params.append(user_row["slug"])
            else:
                clauses.append("1 = 0")

        rows = db.execute(
            f"""
            SELECT
                COALESCE(sp.repo_relative_path, sp.relative_path, sp.display_path) AS display_path,
                sp.relative_path,
                sp.repo_relative_path,
                s.repo_name,
                COUNT(DISTINCT sp.session_id) AS sessions,
                SUM(sp.occurrence_count) AS occurrences,
                SUM(CASE WHEN sp.operation = 'write' THEN sp.occurrence_count ELSE 0 END) AS writes,
                SUM(CASE WHEN sp.operation = 'read' THEN sp.occurrence_count ELSE 0 END) AS reads,
                SUM(CASE WHEN sp.operation = 'search' THEN sp.occurrence_count ELSE 0 END) AS searches
            FROM session_paths sp
            JOIN session_catalog s ON s.session_id = sp.session_id
            LEFT JOIN user_catalog u ON u.slug = s.user_slug
            WHERE {' AND '.join(clauses)}
            GROUP BY COALESCE(sp.repo_relative_path, sp.relative_path, sp.display_path), sp.relative_path, sp.repo_relative_path, s.repo_name
            ORDER BY sessions DESC, occurrences DESC, display_path
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.route("/api/users")
    def api_users():
        db = _db()
        rows = db.execute(
            f"""
            SELECT
                u.slug,
                u.username,
                COALESCE(u.display_name, u.username) AS display_name,
                u.bio,
                u.avatar_url,
                u.profile_visibility,
                COUNT(s.session_id) AS sessions,
                SUM(s.user_message_count + s.assistant_message_count) AS messages,
                SUM(s.tool_call_count) AS tool_calls,
                SUM(s.total_input_tokens + s.total_output_tokens) AS tokens,
                MAX(s.last_timestamp) AS last_seen
            FROM user_catalog u
            LEFT JOIN session_catalog s ON s.user_slug = u.slug AND { _listed_session_clause("s", "u") }
            WHERE { _listed_profile_clause("u") }
            GROUP BY u.slug, u.username, u.display_name, u.bio, u.avatar_url, u.profile_visibility
            ORDER BY last_seen DESC, sessions DESC, u.slug
            """
        ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.route("/api/users/<slug>")
    def api_user_profile(slug):
        profile = _build_user_profile(slug)
        if not profile:
            abort(404)
        return jsonify(
            {
                "user": _serialize_user(profile["user"]),
                "summary": dict(profile["summary"]),
            }
        )

    @app.route("/api/users/<slug>/stats")
    def api_user_stats(slug):
        profile = _build_user_profile(slug)
        if not profile:
            abort(404)
        return jsonify(
            {
                "user": _serialize_user(profile["user"]),
                "summary": dict(profile["summary"]),
                "activity": profile["activity"],
                "sources": [dict(row) for row in profile["source_rows"]],
                "top_tools": [dict(row) for row in profile["tool_rows"]],
                "models": [dict(row) for row in profile["model_rows"]],
            }
        )

    @app.route("/api/users/<slug>/sessions")
    def api_user_sessions(slug):
        db = _db()
        user = _get_user_catalog(slug)
        if not _profile_is_directly_visible(user):
            abort(404)

        limit = _parse_int_arg("limit", 50, minimum=1, maximum=200)
        offset = _parse_int_arg("offset", 0, minimum=0)
        project = request.args.get("project", "").strip()
        repo = request.args.get("repo", "").strip()
        branch = request.args.get("branch", "").strip()
        activity = request.args.get("activity", "").strip()
        status_filter = request.args.get("status", "").strip()
        path_query = request.args.get("path", "").strip()
        clauses = [_profile_session_clause("s"), "s.user_slug = ?"]
        params: list = [user["slug"]]
        if project:
            clauses.append("s.project = ?")
            params.append(project)
        if repo:
            clauses.append("COALESCE(s.repo_name, '') = ?")
            params.append(repo)
        if branch:
            clauses.append("COALESCE(s.git_branch, '') = ?")
            params.append(branch)
        if activity:
            clauses.append(_activity_clause(activity, "s"))
        if status_filter:
            clauses.append("COALESCE(s.session_status, 'exploration') = ?")
            params.append(status_filter)
        if path_query:
            clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM session_paths sp
                    WHERE sp.session_id = s.session_id
                      AND (
                          sp.display_path LIKE ?
                          OR COALESCE(sp.relative_path, '') LIKE ?
                          OR COALESCE(sp.repo_relative_path, '') LIKE ?
                      )
                )
                """
            )
            params.extend([f"%{path_query}%", f"%{path_query}%", f"%{path_query}%"])
        total = db.execute(
            f"""
            SELECT COUNT(*)
            FROM session_catalog s
            WHERE {' AND '.join(clauses)}
            """,
            params,
        ).fetchone()[0]
        rows = db.execute(
            f"""
            SELECT
                session_id,
                source,
                project,
                repo_name,
                model,
                session_goal,
                session_summary,
                session_outcome,
                session_status,
                workspace_root,
                worktree_root,
                repo_root,
                git_branch,
                git_commit,
                git_dirty,
                visibility,
                first_timestamp,
                last_timestamp,
                duration_seconds,
                user_message_count,
                assistant_message_count,
                tool_call_count,
                error_count,
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
                total_input_tokens,
                total_output_tokens
            FROM session_catalog s
            WHERE {' AND '.join(clauses)}
            ORDER BY first_timestamp DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
        return jsonify(
            {
                "user": _serialize_user(user),
                "total": total,
                "limit": limit,
                "offset": offset,
                "sessions": [_serialize_session(row) for row in rows],
            }
        )

    @app.route("/api/users/<slug>/rules")
    def api_user_rules(slug):
        if _is_public_mode():
            abort(404)

        db = _db()
        user = get_user_by_identifier(db, slug)
        if not user:
            abort(404)

        rows = list_visibility_rules(db, user["slug"])
        return jsonify(
            {
                "user": _serialize_user(user),
                "rules": [
                    {
                        "id": row["id"],
                        "source_scope": row["source_scope"],
                        "field": row["field"],
                        "match_mode": row["match_mode"],
                        "pattern": row["pattern"],
                        "visibility": row["visibility"],
                        "priority": row["priority"],
                        "threshold": row["threshold"],
                        "enabled": bool(row["enabled"]),
                    }
                    for row in rows
                ],
            }
        )

    @app.route("/api/publish/queue")
    def api_publish_queue():
        if _is_public_mode():
            abort(404)
        from ..publish import list_publish_candidates, serialize_publish_candidate

        limit = _parse_int_arg("limit", 25, minimum=1, maximum=200)
        visibility = request.args.get("visibility", "pending").strip() or "pending"
        status_filter = request.args.get("status", "").strip() or None
        user = request.args.get("user", "").strip() or None
        include_reviews = request.args.get("reviews", "").strip().lower() in {"1", "true", "yes"}
        try:
            candidates = list_publish_candidates(
                _db(),
                user_identifier=user,
                visibility=visibility,
                status=status_filter,
                limit=limit,
                include_reviews=include_reviews,
            )
        except ValueError as exc:
            abort(400, description=str(exc))
        return jsonify(
            {
                "total": len(candidates),
                "limit": limit,
                "visibility": visibility,
                "status": status_filter,
                "reviews": include_reviews,
                "candidates": [serialize_publish_candidate(candidate) for candidate in candidates],
            }
        )

    @app.route("/api/publish/review/<session_id>")
    def api_publish_review(session_id):
        if _is_public_mode():
            abort(404)
        from ..publish import review_publish_session, serialize_publish_review

        review = review_publish_session(_db(), session_id)
        if not review:
            abort(404)
        return jsonify(serialize_publish_review(review))

    return app
