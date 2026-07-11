"""Click CLI for Logpile."""
import json
import os
import socket
from pathlib import Path

import click


def _default_root() -> Path:
    logpile_root = Path.home() / "logpile"
    legacy_root = Path.home() / "agentus"
    if logpile_root.exists():
        return logpile_root
    if legacy_root.exists():
        return legacy_root
    return logpile_root


def _default_db(root: Path) -> Path:
    preferred = root / "logpile.db"
    legacy = root / "agentus.db"
    if preferred.exists():
        return preferred
    if legacy.exists():
        return legacy
    return preferred


DEFAULT_ROOT = _default_root()
DEFAULT_SHARED = DEFAULT_ROOT / "shared"
DEFAULT_DB = _default_db(DEFAULT_ROOT)
BACKEND_CHOICES = ["auto", "local", "cloud"]


def _backend_default() -> str:
    return os.environ.get("LOGPILE_BACKEND", "auto").strip().lower() or "auto"


def _cloud_db_url(db_url: str | None = None) -> str | None:
    return db_url or os.environ.get("LOGPILE_SUPABASE_DB_URL")


def _select_backend(
    backend: str | None,
    *,
    db_path: Path,
    db_url: str | None,
) -> str:
    mode = (backend or _backend_default()).strip().lower()
    if mode not in BACKEND_CHOICES:
        raise click.ClickException(
            f"Unsupported backend '{mode}'. Expected one of: {', '.join(BACKEND_CHOICES)}."
        )
    if mode == "cloud":
        if not db_url:
            raise click.ClickException("Cloud backend requires LOGPILE_SUPABASE_DB_URL or --db-url.")
        return "cloud"
    if mode == "local":
        if not db_path.exists():
            raise click.ClickException(f"Local database not found: {db_path}. Run 'logpile sync' first.")
        return "local"
    if db_url:
        return "cloud"
    if db_path.exists():
        return "local"
    raise click.ClickException(
        "No backend available. Run 'logpile sync' for local mode, or set "
        "LOGPILE_SUPABASE_DB_URL for cloud mode."
    )


def _excerpt(text: str, query: str, *, width: int = 600) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= width:
        return clean
    index = clean.lower().find(query.lower())
    if index < 0:
        return clean[:width].rstrip() + "..."
    start = max(0, index - width // 3)
    end = min(len(clean), start + width)
    prefix = "..." if start else ""
    suffix = "..." if end < len(clean) else ""
    return prefix + clean[start:end].strip() + suffix


def _local_search(db_path: Path, query: str, *, limit: int) -> list[dict]:
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    pattern = f"%{query}%"
    rows = conn.execute(
        """
        SELECT
            s.session_id,
            s.source,
            s.username,
            s.project,
            s.source_path,
            s.shared_path,
            s.first_timestamp,
            s.first_user_message,
            s.session_goal,
            s.session_summary
        FROM session_catalog s
        WHERE s.first_user_message LIKE ?
           OR s.session_goal LIKE ?
           OR s.session_summary LIKE ?
           OR EXISTS (
                SELECT 1 FROM tool_calls tc
                WHERE tc.session_id = s.session_id
                  AND (tc.command LIKE ? OR tc.tool_name LIKE ?)
           )
        ORDER BY s.first_timestamp DESC
        LIMIT ?
        """,
        (pattern, pattern, pattern, pattern, pattern, limit),
    ).fetchall()

    results: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        text = row["first_user_message"] or row["session_goal"] or row["session_summary"] or ""
        results.append(
            {
                "session_id": row["session_id"],
                "source": row["source"],
                "relative_path": row["shared_path"] or row["source_path"],
                "event_index": None,
                "role": "metadata",
                "excerpt": _excerpt(text, query),
            }
        )
        seen.add(row["session_id"])

    if len(results) >= limit:
        conn.close()
        return results[:limit]

    candidates = conn.execute(
        """
        SELECT session_id, source, source_path, shared_path, first_timestamp
        FROM session_catalog
        ORDER BY first_timestamp DESC
        """
    ).fetchall()
    conn.close()

    for row in candidates:
        if len(results) >= limit:
            break
        if row["session_id"] in seen:
            continue
        paths = [row["shared_path"], row["source_path"]]
        for raw_path in paths:
            if not raw_path:
                continue
            path = Path(raw_path)
            if not path.exists() or not path.is_file():
                continue
            try:
                with path.open(encoding="utf-8", errors="replace") as fh:
                    for line_number, line in enumerate(fh, 1):
                        if query.lower() not in line.lower():
                            continue
                        results.append(
                            {
                                "session_id": row["session_id"],
                                "source": row["source"],
                                "relative_path": str(path),
                                "event_index": line_number,
                                "role": "raw",
                                "excerpt": _excerpt(line, query),
                            }
                        )
                        seen.add(row["session_id"])
                        break
            except OSError:
                continue
            if row["session_id"] in seen:
                break
    return results[:limit]


def _local_status(db_path: Path) -> dict:
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sessions = conn.execute(
        """
        SELECT
            COUNT(*) AS count,
            COALESCE(SUM(user_message_count), 0) AS user_messages,
            COALESCE(SUM(assistant_message_count), 0) AS assistant_messages,
            COALESCE(SUM(tool_call_count), 0) AS tool_calls
        FROM sessions
        """
    ).fetchone()
    sources = conn.execute(
        """
        SELECT source, COUNT(*) AS count
        FROM sessions
        GROUP BY source
        ORDER BY count DESC
        """
    ).fetchall()
    conn.close()
    return {
        "sessions": dict(sessions),
        "sources": [dict(row) for row in sources],
    }


def _prepare_db(db: str | Path) -> Path:
    from .db import init_db

    db_path = Path(db)
    init_db(db_path)
    return db_path


def _resolve_sync_username(db: str | Path, requested_username: str | None) -> str:
    from .db import get_db, normalize_username

    explicit = requested_username or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    normalized = normalize_username(explicit)
    db_path = _prepare_db(db)
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT username FROM users ORDER BY updated_at DESC, username").fetchall()
    if requested_username is None and len(rows) == 1:
        return rows[0]["username"]
    return normalized


@click.group()
def cli():
    """Logpile — searchable Claude Code and Codex session logs."""


@cli.command(name="status")
@click.option("--backend", type=click.Choice(BACKEND_CHOICES), default=None,
              help="Read from local SQLite, cloud Postgres, or auto-detect.")
@click.option("--db", default=str(DEFAULT_DB), show_default=True,
              help="SQLite database path for local mode.")
@click.option("--db-url", envvar="LOGPILE_SUPABASE_DB_URL",
              help="Supabase/Postgres connection URL for cloud mode.")
@click.option("--json", "json_output", is_flag=True, help="Emit structured JSON output.")
def status_command(backend, db, db_url, json_output):
    """Show which Logpile backend is active and how much it contains."""
    db_path = Path(db)
    db_url = _cloud_db_url(db_url)
    try:
        selected = _select_backend(backend, db_path=db_path, db_url=db_url)
        if selected == "cloud":
            from .backup import SupabaseArchive, format_bytes

            payload = {"backend": "cloud", **SupabaseArchive(db_url).status()}
            if json_output:
                click.echo(json.dumps(payload, default=str))
                return
            objects = payload["objects"]
            files = payload["files"]
            chunks = payload["chunks"]
            click.echo("backend: cloud")
            click.echo(
                f"objects: {objects['verified_count']}/{objects['count']} verified, "
                f"{format_bytes(int(objects['verified_bytes']))} verified"
            )
            click.echo(
                f"files: {files['count']} rows, {format_bytes(int(files['bytes']))}, "
                f"{files['sources']} source(s)"
            )
            click.echo(
                f"chunks: {chunks['count']} chunks, "
                f"{chunks['sessions']} session(s), {chunks['files']} file(s)"
            )
            return

        payload = {"backend": "local", **_local_status(db_path)}
        if json_output:
            click.echo(json.dumps(payload, default=str))
            return
        sessions = payload["sessions"]
        click.echo("backend: local")
        click.echo(
            f"sessions: {sessions['count']} sessions, "
            f"{sessions['user_messages']} user messages, "
            f"{sessions['assistant_messages']} assistant messages, "
            f"{sessions['tool_calls']} tool calls"
        )
        for row in payload["sources"]:
            click.echo(f"  {row['source']}: {row['count']}")
    except (RuntimeError, click.ClickException) as exc:
        raise click.ClickException(str(exc))


@cli.command(name="search")
@click.argument("query")
@click.option("--backend", type=click.Choice(BACKEND_CHOICES), default=None,
              help="Read from local SQLite/shared files, cloud Postgres, or auto-detect.")
@click.option("--db", default=str(DEFAULT_DB), show_default=True,
              help="SQLite database path for local mode.")
@click.option("--db-url", envvar="LOGPILE_SUPABASE_DB_URL",
              help="Supabase/Postgres connection URL for cloud mode.")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--json", "json_output", is_flag=True, help="Emit structured JSON output.")
def search_command(query, backend, db, db_url, limit, json_output):
    """Search sessions using the selected backend."""
    db_path = Path(db)
    db_url = _cloud_db_url(db_url)
    try:
        selected = _select_backend(backend, db_path=db_path, db_url=db_url)
        if selected == "cloud":
            from .backup import SupabaseArchive

            rows = SupabaseArchive(db_url).search(query, limit=limit)
        else:
            rows = _local_search(db_path, query, limit=limit)
    except RuntimeError as exc:
        raise click.ClickException(str(exc))

    payload = {"backend": selected, "query": query, "results": rows}
    if json_output:
        click.echo(json.dumps(payload, default=str))
        return

    for row in rows:
        session = row.get("session_id") or "unknown-session"
        location = row.get("relative_path") or row.get("source_path") or ""
        if row.get("event_index") is not None:
            location = f"{location}:{row.get('event_index')}"
            if row.get("fragment_index") is not None:
                location += f".{row.get('fragment_index')}.{row.get('chunk_index')}"
        role = row.get("role") or "record"
        click.echo(f"{session}  {location}  [{role}]")
        click.echo((row.get("excerpt") or "").strip())
        click.echo("")


def _print_turn(turn: dict, *, index: int) -> None:
    timestamp = turn.get("timestamp") or ""
    kind = turn.get("type") or "record"
    header = f"{index:04d} {kind}"
    if timestamp:
        header += f" {timestamp}"
    click.echo(header)
    if kind == "assistant":
        for block in turn.get("blocks", []):
            if block.get("type") == "tool_use":
                click.echo(f"  tool: {block.get('name')}")
                click.echo(json.dumps(block.get("input") or {}, ensure_ascii=False)[:5000])
            else:
                click.echo((block.get("text") or "").strip())
    elif kind == "tool_use":
        click.echo(f"  tool: {turn.get('name')}")
        click.echo(json.dumps(turn.get("input") or {}, ensure_ascii=False)[:5000])
    else:
        content = turn.get("content") or turn.get("text") or ""
        click.echo(str(content).strip())
    click.echo("")


@cli.command(name="show")
@click.argument("session_id")
@click.option("--backend", type=click.Choice(BACKEND_CHOICES), default=None,
              help="Read from local SQLite/shared files, cloud Postgres, or auto-detect.")
@click.option("--db", default=str(DEFAULT_DB), show_default=True,
              help="SQLite database path for local mode.")
@click.option("--db-url", envvar="LOGPILE_SUPABASE_DB_URL",
              help="Supabase/Postgres connection URL for cloud mode.")
@click.option("--limit", default=200, show_default=True, type=int,
              help="Maximum turns/chunks to print.")
@click.option("--json", "json_output", is_flag=True, help="Emit structured JSON output.")
def show_command(session_id, backend, db, db_url, limit, json_output):
    """Show a session transcript from local files or cloud-indexed raw chunks."""
    db_path = Path(db)
    db_url = _cloud_db_url(db_url)
    try:
        selected = _select_backend(backend, db_path=db_path, db_url=db_url)
        if selected == "cloud":
            from .backup import SupabaseArchive

            chunks = SupabaseArchive(db_url).session_chunks(session_id, limit=limit)
            if not chunks:
                raise click.ClickException(f"No cloud chunks found for session '{session_id}'.")
            payload = {"backend": "cloud", "session_id": chunks[0]["session_id"], "chunks": chunks}
            if json_output:
                click.echo(json.dumps(payload, default=str))
                return
            click.echo(f"session: {payload['session_id']}  backend: cloud")
            click.echo("")
            for row in chunks:
                location = (
                    f"{row.get('relative_path')}:{row.get('event_index')}"
                    f".{row.get('fragment_index')}.{row.get('chunk_index')}"
                )
                role = row.get("role") or "record"
                tool = f" {row.get('tool_name')}" if row.get("tool_name") else ""
                click.echo(f"{location} [{role}{tool}]")
                click.echo((row.get("content") or "").strip())
                click.echo("")
            return

        import sqlite3
        from .parsers import render_claudecode_transcript, render_codex_transcript

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM session_catalog
            WHERE session_id = ?
               OR session_id LIKE ?
            ORDER BY session_id
            LIMIT 2
            """,
            (session_id, f"{session_id}%"),
        ).fetchall()
        conn.close()
        if len(rows) != 1:
            raise click.ClickException(f"No unique local session found for '{session_id}'.")
        row = rows[0]
        path = None
        for candidate in (row["shared_path"], row["source_path"]):
            if candidate and Path(candidate).exists():
                path = Path(candidate)
                break
        if path is None:
            raise click.ClickException(
                f"Local transcript file is missing for session '{row['session_id']}'."
            )
        turns = (
            render_codex_transcript(path)
            if row["source"] == "codex"
            else render_claudecode_transcript(path)
        )[:limit]
        payload = {"backend": "local", "session_id": row["session_id"], "path": str(path), "turns": turns}
        if json_output:
            click.echo(json.dumps(payload, default=str))
            return
        click.echo(f"session: {row['session_id']}  backend: local  path: {path}")
        click.echo("")
        for index, turn in enumerate(turns, 1):
            _print_turn(turn, index=index)
    except RuntimeError as exc:
        raise click.ClickException(str(exc))


@cli.command()
@click.option("--shared", default=str(DEFAULT_SHARED), show_default=True,
              help="Shared directory path")
@click.option("--db", default=str(DEFAULT_DB), show_default=True,
              help="SQLite database path")
@click.option("--backend", type=click.Choice(["local", "cloud", "both"]), default="local",
              show_default=True, help="Local SQLite sync, cloud raw upload, or both.")
@click.option("--db-url", envvar="LOGPILE_SUPABASE_DB_URL",
              help="Supabase/Postgres connection URL for cloud sync.")
@click.option("--bucket", envvar="LOGPILE_R2_BUCKET", help="R2/S3 bucket for cloud sync.")
@click.option("--endpoint-url", envvar="LOGPILE_R2_ENDPOINT_URL", help="S3-compatible endpoint URL.")
@click.option("--account-id", envvar="LOGPILE_R2_ACCOUNT_ID", help="Cloudflare account id for R2 endpoint construction.")
@click.option("--access-key-id", envvar="LOGPILE_R2_ACCESS_KEY_ID", hidden=True)
@click.option("--secret-access-key", envvar="LOGPILE_R2_SECRET_ACCESS_KEY", hidden=True)
@click.option("--index/--no-index", "index_text", default=True, show_default=True,
              help="Index exact JSONL chunks in cloud mode.")
@click.option("--username", default=None, help="Override system username")
@click.option("--machine", default=None, help="Override machine/hostname")
@click.option("-v", "--verbose", is_flag=True, help="Print each file processed")
def sync(
    shared,
    db,
    backend,
    db_url,
    bucket,
    endpoint_url,
    account_id,
    access_key_id,
    secret_access_key,
    index_text,
    username,
    machine,
    verbose,
):
    """Index local sessions, upload raw logs to cloud storage, or both."""
    if backend in {"local", "both"}:
        from .sync import sync_sessions

        username = _resolve_sync_username(db, username)
        machine = machine or socket.gethostname()
        click.echo(f"Syncing local sessions for {username}@{machine}…")
        new, updated, skipped = sync_sessions(
            shared_dir=Path(shared),
            db_path=Path(db),
            username=username,
            machine=machine,
            home=Path.home(),
            verbose=verbose,
        )
        click.echo(f"Local done: {new} new, {updated} updated, {skipped} unchanged/skipped")

    if backend in {"cloud", "both"}:
        if not db_url:
            raise click.ClickException("Cloud sync requires LOGPILE_SUPABASE_DB_URL or --db-url.")
        from .backup import push_backup, r2_config_from_env

        storage_config = r2_config_from_env(
            bucket=bucket,
            endpoint_url=endpoint_url,
            account_id=account_id,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
        )
        click.echo("Syncing raw logs to cloud…")
        result = push_backup(
            home=Path.home(),
            db_url=db_url,
            storage_config=storage_config,
            index_text=index_text,
        )
        plan = result["plan"]
        click.echo(
            f"Cloud done: {plan['file_count']} file(s), {plan['total_human']}; "
            f"uploaded {result['uploaded']}, indexed {result['indexed_chunks']} chunk(s)."
        )


@cli.command()
@click.option("--shared", default=str(DEFAULT_SHARED), show_default=True)
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=5002, show_default=True, type=int)
@click.option("--public", is_flag=True,
              help="Serve a public read-only view (no auth required). "
                   "Private/redacted sessions are still hidden.")
@click.option("--dev", is_flag=True, help="Run Next.js dev server with HMR (default: production)")
@click.option("--flask", is_flag=True, hidden=True,
              help="Use the legacy Flask server instead of Next.js")
def serve(shared, db, host, port, public, dev, flask):
    """Start the Logpile web viewer.

    Uses the Next.js frontend (web/) by default.
    Falls back to the legacy Flask server with --flask.
    """
    db_path = Path(db)
    if not db_path.exists():
        click.echo("Database not found. Run 'logpile sync' first.", err=True)
        raise SystemExit(1)

    if flask:
        # Legacy Flask path
        from .web.app import create_app
        app = create_app(db_path=db_path, shared_dir=Path(shared), public_mode=public)
        mode = "PUBLIC read-only" if public else "private"
        click.echo(f"Logpile Flask ({mode}) at http://{host}:{port}")
        app.run(host=host, port=port)
        return

    # ── Next.js path ──────────────────────────────────────────────────
    import shutil
    import subprocess
    import signal
    import sys

    # Find web/ directory relative to the package
    pkg_dir = Path(__file__).resolve().parent.parent
    web_dir = pkg_dir / "web"
    if not web_dir.exists():
        click.echo(
            f"Next.js app not found at {web_dir}.\n"
            "Either run from the logpile repo, or use --flask for the legacy server.",
            err=True,
        )
        raise SystemExit(1)

    # Check for bun
    bun = shutil.which("bun")
    if not bun:
        click.echo(
            "bun not found. Install it: https://bun.sh\n"
            "Or use --flask for the legacy Flask server.",
            err=True,
        )
        raise SystemExit(1)

    # Bootstrap: install deps if node_modules missing
    if not (web_dir / "node_modules").exists():
        click.echo("Installing Next.js dependencies…")
        subprocess.run([bun, "install"], cwd=str(web_dir), check=True)

    # Build for production mode
    if not dev:
        click.echo("Building Next.js app…")
        subprocess.run(
            [bun, "run", "build"],
            cwd=str(web_dir),
            check=True,
            env={
                **os.environ,
                "LOGPILE_DB_PATH": str(db_path.resolve()),
                "LOGPILE_SHARED_DIR": str(Path(shared).resolve()),
                "LOGPILE_PUBLIC_MODE": "true" if public else "false",
                "LOGPILE_PYTHON_BIN": sys.executable,
            },
        )

    # Start server
    mode = "PUBLIC" if public else "private"
    cmd_name = "dev" if dev else "start"
    click.echo(f"Logpile ({mode}) at http://{host}:{port}")

    env = {
        **os.environ,
        "LOGPILE_DB_PATH": str(db_path.resolve()),
        "LOGPILE_SHARED_DIR": str(Path(shared).resolve()),
        "LOGPILE_PUBLIC_MODE": "true" if public else "false",
        "LOGPILE_PYTHON_BIN": sys.executable,
    }

    cmd = [bun, "run", cmd_name, "--port", str(port), "--hostname", host]

    proc = subprocess.Popen(cmd, cwd=str(web_dir), env=env)

    def _shutdown(signum=None, frame=None):
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        sys.exit(proc.wait())
    except KeyboardInterrupt:
        _shutdown()


@cli.command()
@click.argument("session_id")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--shared", default=str(DEFAULT_SHARED), show_default=True)
def private(session_id, db, shared):
    """Mark a session as private (content hidden from all viewers)."""
    from .db import get_db, set_session_visibility
    with get_db(_prepare_db(db)) as conn:
        try:
            count = set_session_visibility(
                conn,
                session_id,
                "private",
                shared_dir=Path(shared),
            )
        except ValueError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1)
        click.echo(
            f"Marked {count} session(s) as private"
            if count
            else f"No session found matching '{session_id}'"
        )


@cli.command()
@click.argument("session_id")
@click.argument("level", type=click.Choice(["private", "unlisted", "public"]))
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--shared", default=str(DEFAULT_SHARED), show_default=True)
def visibility(session_id, level, db, shared):
    """Set session visibility."""
    from .db import get_db, set_session_visibility

    with get_db(_prepare_db(db)) as conn:
        try:
            count = set_session_visibility(
                conn,
                session_id,
                level,
                shared_dir=Path(shared),
            )
        except ValueError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1)
        click.echo(
            f"Updated {count} session(s) to visibility={level}"
            if count
            else f"No session found matching '{session_id}'"
        )


@cli.command()
@click.argument("session_id")
@click.argument("turn_number", type=int)
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--shared", default=str(DEFAULT_SHARED), show_default=True)
def redact(session_id, turn_number, db, shared):
    """Redact a turn from a session.

    Currently marks the whole session private.
    Per-turn redaction will write a sidecar .redact file in a future version.
    """
    from .db import get_db, set_session_visibility
    with get_db(_prepare_db(db)) as conn:
        try:
            count = set_session_visibility(
                conn,
                session_id,
                "private",
                shared_dir=Path(shared),
            )
        except ValueError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1)
        if count:
            click.echo(f"Session {session_id} marked private (turn {turn_number} redacted).")
        else:
            click.echo(f"No session found matching '{session_id}'")


@cli.command(name="users")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
def users_command(db):
    """List known users."""
    from .db import get_db, list_users

    with get_db(_prepare_db(db)) as conn:
        rows = list_users(conn)
        if not rows:
            click.echo("No users found.")
            return
        for row in rows:
            click.echo(
                f"{row['username']}\t"
                f"profile={row['profile_visibility']}\tdefault={row['default_session_visibility']}"
            )


@cli.command(name="user")
@click.argument("identifier")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--display-name", default=None)
@click.option("--bio", default=None)
@click.option("--avatar-url", default=None)
@click.option(
    "--profile-visibility",
    type=click.Choice(["private", "unlisted", "public"]),
    default=None,
)
@click.option(
    "--default-session-visibility",
    type=click.Choice(["private", "unlisted", "public"]),
    default=None,
)
@click.option("--github", "github_username", default=None,
              help="Link a GitHub username for `logpile github sync`")
def user_command(
    identifier,
    db,
    display_name,
    bio,
    avatar_url,
    profile_visibility,
    default_session_visibility,
    github_username,
):
    """Update user metadata."""
    from .db import get_db, update_user

    with get_db(_prepare_db(db)) as conn:
        row = update_user(
            conn,
            identifier,
            display_name=display_name,
            bio=bio,
            avatar_url=avatar_url,
            profile_visibility=profile_visibility,
            default_session_visibility=default_session_visibility,
            github_username=github_username,
        )
        if not row:
            click.echo(f"No user found matching '{identifier}'")
            raise SystemExit(1)
        click.echo(
            f"Updated {row['username']} "
            f"(profile={row['profile_visibility']}, default={row['default_session_visibility']})"
        )


@cli.group(name="github")
def github_group():
    """GitHub activity sync for operator profiles."""


@github_group.command("sync")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--user", "identifier", default=None,
              help="Sync one user (by username). Otherwise syncs everyone with a linked GitHub handle.")
@click.option("--since", default=None,
              help="ISO date (YYYY-MM-DD). Default: 540 days ago.")
def github_sync(db, identifier, since):
    """Pull GitHub contribution data into `user_github_daily`.

    Requires GITHUB_TOKEN env var or `gh auth login`. Link handles first:

        logpile user <username> --github <github-handle>
    """
    from datetime import datetime, timezone
    from .db import get_db, get_user_by_identifier
    from .github import sync_user_github, users_with_github, GitHubSyncError

    since_dt = None
    if since:
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            click.echo(f"Invalid --since (want YYYY-MM-DD): {since}", err=True)
            raise SystemExit(1)

    with get_db(_prepare_db(db)) as conn:
        if identifier:
            user = get_user_by_identifier(conn, identifier)
            if not user:
                click.echo(f"No user found matching '{identifier}'", err=True)
                raise SystemExit(1)
            gh = user["github_username"] if "github_username" in user.keys() else None
            if not gh:
                click.echo(
                    f"User '{user['username']}' has no linked GitHub handle. "
                    f"Run: logpile user {user['username']} --github <handle>",
                    err=True,
                )
                raise SystemExit(1)
            targets = [(user["username"], gh)]
        else:
            targets = users_with_github(conn)
            if not targets:
                click.echo(
                    "No users have a linked GitHub handle. "
                    "Link one with: logpile user <username> --github <handle>",
                    err=True,
                )
                raise SystemExit(1)

        for username, gh_user in targets:
            click.echo(f"Syncing GitHub for {username} ({gh_user})…")
            try:
                stats = sync_user_github(conn, username=username, github_user=gh_user, since=since_dt)
            except GitHubSyncError as exc:
                click.echo(f"  ✗ {exc}", err=True)
                continue
            click.echo(
                f"  ✓ {stats['days_synced']} days, "
                f"{stats['total_contributions']} contributions, "
                f"{stats['total_prs']} PRs opened (since {stats['since']})"
            )


@cli.group(name="rules")
def rules_group():
    """Manage automatic session visibility rules."""


@rules_group.command("list")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--user", "identifier", default=None, help="Filter by username")
def rules_list(db, identifier):
    """List visibility rules."""
    from .db import get_db, list_visibility_rules

    with get_db(_prepare_db(db)) as conn:
        rows = list_visibility_rules(conn, identifier)
        if not rows:
            click.echo("No rules found.")
            return
        for row in rows:
            source_scope = row["source_scope"] or "*"
            threshold = (
                f"{row['threshold']:.2f}" if row["threshold"] is not None else "—"
            )
            state = "enabled" if row["enabled"] else "disabled"
            click.echo(
                f"{row['id']}\t{row['username']}\t{row['visibility']}\t"
                f"{row['field']}:{row['match_mode']}\t{row['pattern']}\t"
                f"source={source_scope}\tpriority={row['priority']}\t"
                f"threshold={threshold}\t{state}"
            )


@rules_group.command("add")
@click.argument("identifier")
@click.option(
    "--field",
    required=True,
    type=click.Choice(
        ["project", "source_path", "first_user_message", "model", "machine", "username"]
    ),
)
@click.option(
    "--mode",
    "match_mode",
    required=True,
    type=click.Choice(["equals", "contains", "prefix", "suffix", "regex", "fuzzy"]),
)
@click.option("--pattern", required=True)
@click.option(
    "--visibility",
    required=True,
    type=click.Choice(["private", "unlisted", "public"]),
)
@click.option("--priority", default=100, show_default=True, type=int)
@click.option("--threshold", default=None, type=float)
@click.option(
    "--source-scope",
    default=None,
    type=click.Choice(["claudecode", "codex"]),
    help="Only apply this rule to one agent source",
)
@click.option("--disabled", is_flag=True, help="Create the rule in a disabled state")
@click.option("--apply", is_flag=True, help="Recompute matching sessions after adding the rule")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--shared", default=str(DEFAULT_SHARED), show_default=True)
def rules_add(
    identifier,
    field,
    match_mode,
    pattern,
    visibility,
    priority,
    threshold,
    source_scope,
    disabled,
    apply,
    db,
    shared,
):
    """Add an automatic visibility rule."""
    from .db import create_visibility_rule, get_db, recompute_session_visibility

    with get_db(_prepare_db(db)) as conn:
        row = create_visibility_rule(
            conn,
            identifier,
            field=field,
            match_mode=match_mode,
            pattern=pattern,
            visibility=visibility,
            priority=priority,
            threshold=threshold,
            source_scope=source_scope,
            enabled=not disabled,
        )
        if not row:
            click.echo(f"No user found matching '{identifier}'")
            raise SystemExit(1)

        click.echo(
            f"Created rule {row['id']} for {row['username']}: "
            f"{row['visibility']} when {row['field']} {row['match_mode']} '{row['pattern']}'"
        )
        if apply:
            updated = recompute_session_visibility(
                conn,
                identifier=row["username"],
                shared_dir=Path(shared),
            )
            click.echo(f"Recomputed {updated} non-manual session(s).")


@rules_group.command("delete")
@click.argument("rule_id", type=int)
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--shared", default=str(DEFAULT_SHARED), show_default=True)
def rules_delete(rule_id, db, shared):
    """Delete a visibility rule."""
    from .db import delete_visibility_rule, get_db

    with get_db(_prepare_db(db)) as conn:
        count, updated = delete_visibility_rule(
            conn,
            rule_id,
            shared_dir=Path(shared),
        )
        if not count:
            click.echo(f"No rule found with id={rule_id}")
            return
        click.echo(f"Deleted rule {rule_id} and recomputed {updated} session(s).")


@rules_group.command("apply")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--shared", default=str(DEFAULT_SHARED), show_default=True)
@click.option("--user", "identifier", default=None, help="Restrict to one user")
@click.option("--session", "session_id_prefix", default=None, help="Restrict to one session prefix")
@click.option("--include-manual", is_flag=True, help="Also recompute manual overrides")
def rules_apply(db, shared, identifier, session_id_prefix, include_manual):
    """Recompute session visibility from defaults and rules."""
    from .db import get_db, get_user_by_identifier, recompute_session_visibility

    with get_db(_prepare_db(db)) as conn:
        user = get_user_by_identifier(conn, identifier) if identifier else None
        if identifier and not user:
            click.echo(f"No user found matching '{identifier}'")
            raise SystemExit(1)
        updated = recompute_session_visibility(
            conn,
            shared_dir=Path(shared),
            identifier=identifier,
            session_id_prefix=session_id_prefix,
            include_manual=include_manual,
        )
        click.echo(f"Recomputed {updated} session(s).")


@rules_group.command("test")
@click.argument("session_id_prefix")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
def rules_test(session_id_prefix, db):
    """Preview the computed visibility for an existing session."""
    from .db import get_db, preview_session_visibility

    with get_db(_prepare_db(db)) as conn:
        preview = preview_session_visibility(conn, session_id_prefix)
        if not preview:
            click.echo(f"No session found matching '{session_id_prefix}'")
            raise SystemExit(1)

        session = preview["session"]
        decision = preview["decision"]
        click.echo(
            f"{session['session_id']}\tcurrent={session['visibility']} "
            f"({session['visibility_source']})\tpredicted={decision['visibility']} "
            f"({decision['visibility_source']})\t{decision['visibility_reason']}"
        )


@cli.group(name="publish")
def publish_group():
    """Review and publish sessions safely."""


@publish_group.command("queue")
@click.option("--user", default=None, help="Filter by username")
@click.option(
    "--visibility",
    default="pending",
    type=click.Choice(["pending", "needs_changes", "all", "private", "unlisted", "public"]),
    show_default=True,
)
@click.option(
    "--status",
    "status_filter",
    default=None,
    type=click.Choice(["exploration", "success", "partial", "failed"]),
)
@click.option(
    "--origin",
    "origin_filter",
    default=None,
    type=click.Choice(
        [
            "human_direct",
            "human_delegated",
            "pipeline_eval",
            "meta_scaffolding",
            "system_generated",
        ]
    ),
)
@click.option("--limit", default=25, show_default=True, type=int)
@click.option("--reviews/--no-reviews", default=True, show_default=True)
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--json", "json_output", is_flag=True, help="Emit structured JSON output.")
def publish_queue(user, visibility, status_filter, origin_filter, limit, reviews, db, json_output):
    """List candidate sessions for publication."""
    from .db import get_db
    from .publish import (
        count_publish_candidates,
        format_publish_queue,
        list_publish_candidates,
        serialize_publish_candidate,
    )

    with get_db(_prepare_db(db)) as conn:
        total = count_publish_candidates(
            conn,
            user_identifier=user,
            visibility=visibility,
            status=status_filter,
            origin=origin_filter,
        )
        candidates = list_publish_candidates(
            conn,
            user_identifier=user,
            visibility=visibility,
            status=status_filter,
            origin=origin_filter,
            limit=limit,
            include_reviews=reviews,
        )
        if json_output:
            click.echo(
                json.dumps(
                    {
                        "total": total,
                        "limit": limit,
                        "visibility": visibility,
                        "status": status_filter,
                        "origin": origin_filter,
                        "reviews": reviews,
                        "candidates": [
                            serialize_publish_candidate(candidate)
                            for candidate in candidates
                        ],
                    }
                )
            )
            return
        for line in format_publish_queue(candidates):
            click.echo(line)


@cli.group(name="backup")
def backup_group():
    """Back up raw logs and index exact searchable chunks."""


@backup_group.command("schema")
def backup_schema():
    """Print the Supabase/Postgres schema for raw log search."""
    from .backup import SUPABASE_SCHEMA_SQL

    click.echo(SUPABASE_SCHEMA_SQL.strip())


@backup_group.command("plan")
@click.option("--home", default=str(Path.home()), show_default=True)
@click.option("--include-codex-db/--no-codex-db", default=True, show_default=True)
@click.option("--limit", default=None, type=int, help="Only inspect the first N files.")
@click.option("--json", "json_output", is_flag=True, help="Emit structured JSON output.")
def backup_plan(home, include_codex_db, limit, json_output):
    """Show the raw local log files that would be backed up."""
    from .backup import format_bytes, plan_backup, plan_to_dict

    plan = plan_backup(
        home=Path(home),
        include_codex_db=include_codex_db,
        limit=limit,
    )
    if json_output:
        click.echo(json.dumps(plan_to_dict(plan)))
        return

    click.echo(f"{len(plan.candidates)} file(s), {format_bytes(plan.total_bytes)}")
    for source, count in sorted(plan.source_counts.items()):
        click.echo(f"  {source}: {count} file(s), {format_bytes(plan.source_bytes[source])}")


@backup_group.command("push")
@click.option("--home", default=str(Path.home()), show_default=True)
@click.option("--db-url", envvar="LOGPILE_SUPABASE_DB_URL", help="Supabase/Postgres connection URL.")
@click.option("--bucket", envvar="LOGPILE_R2_BUCKET", help="R2/S3 bucket for immutable raw logs.")
@click.option("--endpoint-url", envvar="LOGPILE_R2_ENDPOINT_URL", help="S3-compatible endpoint URL.")
@click.option("--account-id", envvar="LOGPILE_R2_ACCOUNT_ID", help="Cloudflare account id for R2 endpoint construction.")
@click.option("--access-key-id", envvar="LOGPILE_R2_ACCESS_KEY_ID", hidden=True)
@click.option("--secret-access-key", envvar="LOGPILE_R2_SECRET_ACCESS_KEY", hidden=True)
@click.option("--provider", default="r2", show_default=True)
@click.option("--include-codex-db/--no-codex-db", default=True, show_default=True)
@click.option("--index/--no-index", "index_text", default=True, show_default=True,
              help="Index exact JSONL chunks in Postgres after upload.")
@click.option("--missing", "missing_only", is_flag=True,
              help="Skip local files whose sha256 is already present in Postgres.")
@click.option("--defer-search-index", is_flag=True,
              help="Skip creating the FTS index until a later backup search-index run.")
@click.option("--limit", default=None, type=int, help="Only process the first N files.")
@click.option("--dry-run", is_flag=True, help="Plan only; do not connect to cloud services.")
@click.option("--json", "json_output", is_flag=True, help="Emit structured JSON output.")
def backup_push(
    home,
    db_url,
    bucket,
    endpoint_url,
    account_id,
    access_key_id,
    secret_access_key,
    provider,
    include_codex_db,
    index_text,
    missing_only,
    defer_search_index,
    limit,
    dry_run,
    json_output,
):
    """Upload immutable raw logs and index exact text chunks."""
    from .backup import (
        format_bytes,
        plan_backup,
        plan_to_dict,
        push_backup,
        r2_config_from_env,
    )

    if dry_run:
        plan = plan_backup(
            home=Path(home),
            include_codex_db=include_codex_db,
            limit=limit,
        )
        payload = {
            "plan": plan_to_dict(plan),
            "uploaded": 0,
            "indexed_chunks": 0,
            "dry_run": True,
        }
        if json_output:
            click.echo(json.dumps(payload))
            return
        click.echo(
            f"Dry run: {len(plan.candidates)} file(s), {format_bytes(plan.total_bytes)}"
        )
        return

    if not db_url:
        click.echo("Set LOGPILE_SUPABASE_DB_URL or pass --db-url.", err=True)
        raise SystemExit(1)

    try:
        storage_config = r2_config_from_env(
            bucket=bucket,
            endpoint_url=endpoint_url,
            account_id=account_id,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            provider=provider,
        )
        result = push_backup(
            home=Path(home),
            db_url=db_url,
            storage_config=storage_config,
            include_codex_db=include_codex_db,
            limit=limit,
            index_text=index_text,
            missing_only=missing_only,
            create_search_index=not defer_search_index,
        )
    except (RuntimeError, ValueError) as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1)

    if json_output:
        click.echo(json.dumps(result))
        return

    plan = result["plan"]
    click.echo(
        f"Processed {plan['file_count']} file(s), {plan['total_human']}; "
        f"uploaded {result['uploaded']}, indexed {result['indexed_chunks']} chunk(s)."
    )
    if result.get("skipped_existing"):
        click.echo(f"Skipped {result['skipped_existing']} existing file(s).")


@backup_group.command("index")
@click.option("--db-url", envvar="LOGPILE_SUPABASE_DB_URL", help="Supabase/Postgres connection URL.")
@click.option("--bucket", envvar="LOGPILE_R2_BUCKET", help="R2/S3 bucket for immutable raw logs.")
@click.option("--endpoint-url", envvar="LOGPILE_R2_ENDPOINT_URL", help="S3-compatible endpoint URL.")
@click.option("--account-id", envvar="LOGPILE_R2_ACCOUNT_ID", help="Cloudflare account id for R2 endpoint construction.")
@click.option("--access-key-id", envvar="LOGPILE_R2_ACCESS_KEY_ID", hidden=True)
@click.option("--secret-access-key", envvar="LOGPILE_R2_SECRET_ACCESS_KEY", hidden=True)
@click.option("--provider", default="r2", show_default=True)
@click.option("--source", default=None, help="Only index one raw file source, e.g. codex or claudecode.")
@click.option("--from-r2/--from-manifest", "from_r2", default=False, show_default=True,
              help="List content-addressed R2/S3 objects directly instead of using the Postgres manifest.")
@click.option("--prefix", default="raw/sha256/", show_default=True,
              help="Object key prefix for --from-r2.")
@click.option("--defer-search-index", is_flag=True,
              help="For --from-r2 bulk imports, skip creating the FTS index until a later backup search-index run.")
@click.option("--missing/--all", "missing_only", default=True, show_default=True,
              help="Index only files without chunks, or rebuild every matching file.")
@click.option("--limit", default=None, type=int, help="Only index the first N matching files.")
@click.option("--json", "json_output", is_flag=True, help="Emit structured JSON output.")
def backup_index(
    db_url,
    bucket,
    endpoint_url,
    account_id,
    access_key_id,
    secret_access_key,
    provider,
    source,
    from_r2,
    prefix,
    defer_search_index,
    missing_only,
    limit,
    json_output,
):
    """Build searchable chunks from raw objects already stored in R2/S3."""
    if not db_url:
        click.echo("Set LOGPILE_SUPABASE_DB_URL or pass --db-url.", err=True)
        raise SystemExit(1)

    from .backup import index_cloud_backup, index_r2_objects, r2_config_from_env

    try:
        storage_config = r2_config_from_env(
            bucket=bucket,
            endpoint_url=endpoint_url,
            account_id=account_id,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            provider=provider,
        )
        if from_r2:
            result = index_r2_objects(
                db_url=db_url,
                storage_config=storage_config,
                prefix=prefix,
                source=source,
                missing_only=missing_only,
                limit=limit,
                create_search_index=not defer_search_index,
            )
        else:
            result = index_cloud_backup(
                db_url=db_url,
                storage_config=storage_config,
                source=source,
                missing_only=missing_only,
                limit=limit,
            )
    except (RuntimeError, ValueError) as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1)

    if json_output:
        click.echo(json.dumps(result))
        return

    click.echo(
        f"Indexed {result['files_indexed']}/{result.get('candidate_files', result.get('candidate_objects'))} file(s), "
        f"{result['indexed_chunks']} chunk(s), downloaded {result['downloaded_human']}."
    )
    if result["skipped"]:
        click.echo(f"Skipped {result['skipped']} file(s) with size mismatches.")
    if result.get("skipped_existing"):
        click.echo(f"Skipped {result['skipped_existing']} existing object(s).")
    if result.get("skipped_source"):
        click.echo(f"Skipped {result['skipped_source']} object(s) from other sources.")


@backup_group.command("search-index")
@click.option("--db-url", envvar="LOGPILE_SUPABASE_DB_URL", help="Supabase/Postgres connection URL.")
def backup_search_index(db_url):
    """Create the Supabase/Postgres full-text search index after a bulk import."""
    if not db_url:
        click.echo("Set LOGPILE_SUPABASE_DB_URL or pass --db-url.", err=True)
        raise SystemExit(1)

    from .backup import SupabaseArchive

    try:
        SupabaseArchive(db_url).ensure_search_index()
    except RuntimeError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1)

    click.echo("Search index is ready.")


@backup_group.command("search")
@click.argument("query")
@click.option("--db-url", envvar="LOGPILE_SUPABASE_DB_URL", help="Supabase/Postgres connection URL.")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--json", "json_output", is_flag=True, help="Emit structured JSON output.")
def backup_search(query, db_url, limit, json_output):
    """Search exact raw chunks in Supabase/Postgres."""
    if not db_url:
        click.echo("Set LOGPILE_SUPABASE_DB_URL or pass --db-url.", err=True)
        raise SystemExit(1)

    from .backup import SupabaseArchive

    try:
        rows = SupabaseArchive(db_url).search(query, limit=limit)
    except RuntimeError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1)

    if json_output:
        click.echo(json.dumps({"query": query, "results": rows}))
        return

    for row in rows:
        location = (
            f"{row.get('relative_path')}:{row.get('event_index')}"
            f".{row.get('fragment_index')}.{row.get('chunk_index')}"
        )
        click.echo(f"{location} [{row.get('role') or 'record'}]")
        click.echo((row.get("excerpt") or "").strip())
        click.echo("")


@publish_group.command("review")
@click.argument("session_id")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--json", "json_output", is_flag=True, help="Emit structured JSON output.")
def publish_review(session_id, db, json_output):
    """Inspect a session for risky content before publishing."""
    from .publish import (
        format_publish_review,
        review_publish_session,
        serialize_publish_review,
    )
    from .db import get_db

    with get_db(_prepare_db(db)) as conn:
        try:
            review = review_publish_session(conn, session_id)
        except ValueError as exc:
            if json_output:
                click.echo(
                    json.dumps(
                        {
                            "error": str(exc),
                            "code": "ambiguous",
                            "session_id": session_id,
                        }
                    )
                )
            else:
                click.echo(str(exc), err=True)
            raise SystemExit(1)
        if not review:
            if json_output:
                click.echo(
                    json.dumps(
                        {
                            "error": "not found",
                            "code": "not_found",
                            "session_id": session_id,
                        }
                    )
                )
            else:
                click.echo(f"No session found matching '{session_id}'")
            raise SystemExit(1)
        if json_output:
            click.echo(json.dumps(serialize_publish_review(review)))
            return
        for line in format_publish_review(review):
            click.echo(line)


def _publish_apply_impl(session_id, db, shared, visibility, force):
    from .db import get_db, set_session_visibility
    from .publish import (
        can_apply_visibility,
        format_publish_review,
        preserve_reviewed_artifact,
        review_publish_session,
    )

    with get_db(_prepare_db(db)) as conn:
        try:
            review = review_publish_session(conn, session_id)
        except ValueError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1)
        if not review:
            click.echo(f"No session found matching '{session_id}'")
            raise SystemExit(1)

        allowed, reason = can_apply_visibility(review, visibility, force=force)
        if not allowed:
            for line in format_publish_review(review):
                click.echo(line)
            click.echo(reason, err=True)
            raise SystemExit(1)

        count = set_session_visibility(
            conn,
            review.session_id,
            visibility,
            shared_dir=Path(shared),
        )
        if not count:
            click.echo(f"No session found matching '{session_id}'")
            raise SystemExit(1)
        preserve_reviewed_artifact(conn, review)
        click.echo(f"Updated {count} session(s) to visibility={visibility}")


@publish_group.command("approve")
@click.argument("session_id")
@click.option(
    "--visibility",
    default="unlisted",
    type=click.Choice(["private", "unlisted", "public"]),
    show_default=True,
)
@click.option("--force", is_flag=True, help="Override the review recommendation.")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--shared", default=str(DEFAULT_SHARED), show_default=True)
def publish_approve(session_id, visibility, force, db, shared):
    """Approve a reviewed session for publication."""
    _publish_apply_impl(session_id, db, shared, visibility, force)


@publish_group.command("apply")
@click.argument("session_id")
@click.option(
    "--visibility",
    default="unlisted",
    type=click.Choice(["private", "unlisted", "public"]),
    show_default=True,
)
@click.option("--force", is_flag=True, help="Override the review recommendation.")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--shared", default=str(DEFAULT_SHARED), show_default=True)
def publish_apply(session_id, visibility, force, db, shared):
    """Apply the reviewed visibility decision."""
    _publish_apply_impl(session_id, db, shared, visibility, force)


@cli.command()
@click.option("--user", default=None, help="Filter by username (default: all users)")
@click.option("--since", default=None, help="Start date (ISO YYYY-MM-DD)")
@click.option("--until", default=None, help="End date (ISO YYYY-MM-DD)")
@click.option("--json", "json_output", is_flag=True, help="Emit structured JSON output")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
def stats(user, since, until, json_output, db):
    """Print a comprehensive token usage report."""
    from .db import get_db
    from .stats import compute_stats, format_stats

    db_path = _prepare_db(db)
    username = _resolve_sync_username(db, user) if user else None

    with get_db(db_path) as conn:
        data = compute_stats(
            conn,
            username=username,
            since=since,
            until=until,
        )

    if json_output:
        click.echo(json.dumps(data))
        return

    for line in format_stats(data):
        click.echo(line)


if __name__ == "__main__":
    cli()
