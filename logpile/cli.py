"""Click CLI for Logpile."""
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


def _prepare_db(db: str | Path) -> Path:
    from .db import init_db

    db_path = Path(db)
    init_db(db_path)
    return db_path


@click.group()
def cli():
    """Logpile — searchable Claude Code and Codex session logs."""


@cli.command()
@click.option("--shared", default=str(DEFAULT_SHARED), show_default=True,
              help="Shared directory path")
@click.option("--db", default=str(DEFAULT_DB), show_default=True,
              help="SQLite database path")
@click.option("--username", default=None, help="Override system username")
@click.option("--machine", default=None, help="Override machine/hostname")
@click.option("-v", "--verbose", is_flag=True, help="Print each file processed")
def sync(shared, db, username, machine, verbose):
    """Copy sessions to shared directory and rebuild the index."""
    from .sync import sync_sessions
    username = username or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    machine = machine or socket.gethostname()
    click.echo(f"Syncing sessions for {username}@{machine}…")
    new, updated, skipped = sync_sessions(
        shared_dir=Path(shared),
        db_path=Path(db),
        username=username,
        machine=machine,
        home=Path.home(),
        verbose=verbose,
    )
    click.echo(f"Done: {new} new, {updated} updated, {skipped} unchanged/skipped")


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
    """List known users and slug mappings."""
    from .db import get_db, list_users

    with get_db(_prepare_db(db)) as conn:
        rows = list_users(conn)
        if not rows:
            click.echo("No users found.")
            return
        for row in rows:
            click.echo(
                f"{row['slug']}\t{row['username']}\t"
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
def user_command(
    identifier,
    db,
    display_name,
    bio,
    avatar_url,
    profile_visibility,
    default_session_visibility,
):
    """Update user metadata by slug or username."""
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
        )
        if not row:
            click.echo(f"No user found matching '{identifier}'")
            raise SystemExit(1)
        click.echo(
            f"Updated {row['slug']} "
            f"(profile={row['profile_visibility']}, default={row['default_session_visibility']})"
        )


@cli.group(name="rules")
def rules_group():
    """Manage automatic session visibility rules."""


@rules_group.command("list")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
@click.option("--user", "identifier", default=None, help="Filter by user slug or username")
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
                f"{row['id']}\t{row['user_slug']}\t{row['visibility']}\t"
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
            f"Created rule {row['id']} for {row['user_slug']}: "
            f"{row['visibility']} when {row['field']} {row['match_mode']} '{row['pattern']}'"
        )
        if apply:
            updated = recompute_session_visibility(
                conn,
                identifier=row["user_slug"],
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
@click.option("--user", default=None, help="Filter by user slug or username")
@click.option(
    "--visibility",
    default="pending",
    type=click.Choice(["pending", "all", "private", "unlisted", "public"]),
    show_default=True,
)
@click.option(
    "--status",
    "status_filter",
    default=None,
    type=click.Choice(["exploration", "success", "partial", "failed"]),
)
@click.option("--limit", default=25, show_default=True, type=int)
@click.option("--reviews/--no-reviews", default=True, show_default=True)
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
def publish_queue(user, visibility, status_filter, limit, reviews, db):
    """List candidate sessions for publication."""
    from .db import get_db
    from .publish import format_publish_queue, list_publish_candidates

    with get_db(_prepare_db(db)) as conn:
        candidates = list_publish_candidates(
            conn,
            user_identifier=user,
            visibility=visibility,
            status=status_filter,
            limit=limit,
            include_reviews=reviews,
        )
        for line in format_publish_queue(candidates):
            click.echo(line)


@publish_group.command("review")
@click.argument("session_id")
@click.option("--db", default=str(DEFAULT_DB), show_default=True)
def publish_review(session_id, db):
    """Inspect a session for risky content before publishing."""
    from .publish import format_publish_review, review_publish_session
    from .db import get_db

    with get_db(_prepare_db(db)) as conn:
        try:
            review = review_publish_session(conn, session_id)
        except ValueError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1)
        if not review:
            click.echo(f"No session found matching '{session_id}'")
            raise SystemExit(1)
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


if __name__ == "__main__":
    cli()
