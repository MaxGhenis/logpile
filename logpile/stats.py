"""Token usage statistics and session analysis.

Unbounded reports retain whole-session semantics.  When ``since`` or
``until`` is supplied, the report instead has inclusive UTC event-period
semantics: tokens come from ``session_daily_effective`` rows whose day is in
the requested range, and a session is active when it has at least one such
row.  Pattern and repository labels remain properties of the whole session,
while subagent command breakdowns include only tool calls whose own timestamp
falls in the period.
"""

from __future__ import annotations

import calendar
import sqlite3
from collections import defaultdict
from datetime import date, datetime


BEHAVIORAL_PATTERNS = (
    "subagent",
    "marathon",
    "heavy-tooling",
    "long-conversation",
    "medium-conversation",
    "short-task",
)

_SUBAGENT_COMMAND_CATEGORIES = {
    "pr-ops": ("gh pr",),
    "git": ("git ",),
    "testing": ("pytest", "test"),
    "search": ("grep", "find", "rg"),
    "python": ("python", "uv "),
    "review": ("review", "approve"),
    "lint": ("ruff", "lint"),
    "js": ("bun", "npm"),
    "file-read": ("cat", "head", "tail", "nl", "sed"),
}

# ``spawn_depth`` is the canonical classification input.  The remaining
# predicates are defensive evidence for Claude Code rows written before the
# sidechain backfill: real subagent logs live under /subagents/ and sync maps
# their root sessionId to the canonical parent_session_id.
_SUBAGENT_SQL = """(
    COALESCE(s.spawn_depth, 0) > 0
    OR (
        s.source = 'claudecode'
        AND (
            NULLIF(TRIM(s.parent_session_id), '') IS NOT NULL
            OR INSTR(
                '/' || REPLACE(COALESCE(s.source_path, ''), CHAR(92), '/') || '/',
                '/subagents/'
            ) > 0
        )
    )
)"""


def classify_session(
    spawn_depth: int,
    user_message_count: int,
    tool_call_count: int,
    *,
    subagent_evidence: bool = False,
) -> str:
    if spawn_depth > 0 or subagent_evidence:
        return "subagent"
    if user_message_count > 100:
        return "marathon"
    if tool_call_count > 500:
        return "heavy-tooling"
    if 20 <= user_message_count <= 100 and tool_call_count <= 500:
        return "long-conversation"
    if 5 <= user_message_count <= 19:
        return "medium-conversation"
    return "short-task"


def _classify_command(command: str | None) -> str:
    if not command:
        return "other"
    lowered = command.lower()
    for category, prefixes in _SUBAGENT_COMMAND_CATEGORIES.items():
        for prefix in prefixes:
            if lowered.startswith(prefix) or f" {prefix}" in lowered:
                return category
    return "other"


def _build_where(
    username: str | None,
    since: str | None,
    until: str | None,
) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if username:
        clauses.append("s.username = ?")
        params.append(username)
    if since:
        clauses.append("s.first_timestamp >= ?")
        params.append(since)
    if until:
        # Normalize bare dates to end-of-day so "2026-03-15" includes
        # sessions timestamped on March 15.
        if len(until) == 10:
            clauses.append("s.first_timestamp < ?")
            params.append(until + "T99")  # sorts after any valid time
        else:
            clauses.append("s.first_timestamp <= ?")
            params.append(until)
    where = " AND ".join(clauses) if clauses else "1 = 1"
    return where, params


def _has_period_bounds(since: str | None, until: str | None) -> bool:
    return since is not None or until is not None


def _build_daily_where(
    username: str | None,
    since: str | None,
    until: str | None,
) -> tuple[str, list[str]]:
    """Build inclusive UTC-day predicates for session_daily_effective."""
    clauses = ["d.day IS NOT NULL", "d.day != ''"]
    params: list[str] = []
    if username:
        clauses.append("s.username = ?")
        params.append(username)
    if since:
        clauses.append("d.day >= ?")
        params.append(since[:10])
    if until:
        clauses.append("d.day <= ?")
        params.append(until[:10])
    return " AND ".join(clauses), params


def _build_tool_where(
    username: str | None,
    since: str | None,
    until: str | None,
) -> tuple[str, list[str]]:
    """Build predicates using each tool call's timestamp, converted to UTC."""
    clauses: list[str] = []
    params: list[str] = []
    if username:
        clauses.append("s.username = ?")
        params.append(username)
    if since:
        # SQLite date() normalizes ISO-8601 offsets to UTC and returns NULL
        # for missing or malformed timestamps, which excludes those rows.
        clauses.append("date(tc.timestamp) >= ?")
        params.append(since[:10])
    if until:
        clauses.append("date(tc.timestamp) <= ?")
        params.append(until[:10])
    return " AND ".join(clauses) if clauses else "1 = 1", params


def compute_overview(
    conn: sqlite3.Connection,
    *,
    username: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    # Cross-session sums use native_* so a Claude Code resume chain's
    # inherited history counts once, not once per transcript.
    if _has_period_bounds(since, until):
        where, params = _build_daily_where(username, since, until)
        row = conn.execute(
            f"""
            SELECT
                COUNT(DISTINCT d.session_id) AS total_sessions,
                MIN(d.day) AS first_date,
                MAX(d.day) AS last_date,
                COALESCE(SUM(d.native_total_input_tokens), 0) AS total_input_tokens,
                COALESCE(SUM(d.native_total_output_tokens), 0) AS total_output_tokens,
                COALESCE(SUM(d.native_cached_input_tokens), 0) AS cached_input_tokens,
                COALESCE(SUM(d.native_cache_creation_input_tokens), 0) AS cache_creation_input_tokens,
                COUNT(DISTINCT s.repo_name) AS repos_touched
            FROM session_daily_effective d
            JOIN sessions s ON s.session_id = d.session_id
            WHERE {where}
            """,
            params,
        ).fetchone()
    else:
        where, params = _build_where(username, since, until)
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_sessions,
                MIN(s.first_timestamp) AS first_date,
                MAX(s.first_timestamp) AS last_date,
                COALESCE(SUM(s.native_total_input_tokens), 0) AS total_input_tokens,
                COALESCE(SUM(s.native_total_output_tokens), 0) AS total_output_tokens,
                COALESCE(SUM(s.native_cached_input_tokens), 0) AS cached_input_tokens,
                COALESCE(SUM(s.native_cache_creation_input_tokens), 0) AS cache_creation_input_tokens,
                COUNT(DISTINCT s.repo_name) AS repos_touched
            FROM sessions s
            WHERE {where}
            """,
            params,
        ).fetchone()
    return {
        "total_sessions": row["total_sessions"],
        "first_date": row["first_date"],
        "last_date": row["last_date"],
        "total_input_tokens": row["total_input_tokens"],
        "total_output_tokens": row["total_output_tokens"],
        "cached_input_tokens": row["cached_input_tokens"],
        "cache_creation_input_tokens": row["cache_creation_input_tokens"],
        "repos_touched": row["repos_touched"],
    }


def compute_by_pattern(
    conn: sqlite3.Connection,
    *,
    username: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    if _has_period_bounds(since, until):
        where, params = _build_daily_where(username, since, until)
        rows = conn.execute(
            f"""
            SELECT
                s.session_id,
                s.spawn_depth,
                s.user_message_count,
                s.tool_call_count,
                {_SUBAGENT_SQL} AS subagent_evidence,
                COALESCE(SUM(d.native_total_output_tokens), 0) AS total_output_tokens
            FROM session_daily_effective d
            JOIN sessions s ON s.session_id = d.session_id
            WHERE {where}
            GROUP BY
                s.session_id, s.spawn_depth, s.user_message_count,
                s.tool_call_count, subagent_evidence
            """,
            params,
        ).fetchall()
    else:
        where, params = _build_where(username, since, until)
        rows = conn.execute(
            f"""
            SELECT
                s.spawn_depth,
                s.user_message_count,
                s.tool_call_count,
                {_SUBAGENT_SQL} AS subagent_evidence,
                s.native_total_output_tokens AS total_output_tokens
            FROM sessions s
            WHERE {where}
            """,
            params,
        ).fetchall()

    totals: dict[str, dict] = {}
    for pattern in BEHAVIORAL_PATTERNS:
        totals[pattern] = {
            "pattern": pattern,
            "session_count": 0,
            "total_output_tokens": 0,
        }

    grand_total_output = 0
    for row in rows:
        pattern = classify_session(
            row["spawn_depth"] or 0,
            row["user_message_count"] or 0,
            row["tool_call_count"] or 0,
            subagent_evidence=bool(row["subagent_evidence"]),
        )
        output = row["total_output_tokens"] or 0
        totals[pattern]["session_count"] += 1
        totals[pattern]["total_output_tokens"] += output
        grand_total_output += output

    result = []
    for pattern in BEHAVIORAL_PATTERNS:
        entry = totals[pattern]
        count = entry["session_count"]
        entry["avg_output_per_session"] = (
            entry["total_output_tokens"] // count if count else 0
        )
        entry["pct_of_total"] = (
            round(entry["total_output_tokens"] / grand_total_output * 100, 1)
            if grand_total_output
            else 0.0
        )
        result.append(entry)
    return result


def compute_subagent_breakdown(
    conn: sqlite3.Connection,
    *,
    username: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    where, params = _build_tool_where(username, since, until)
    rows = conn.execute(
        f"""
        SELECT tc.command
        FROM tool_calls tc
        JOIN sessions s ON s.session_id = tc.session_id
        WHERE {_SUBAGENT_SQL}
          AND tc.command IS NOT NULL
          AND tc.command != ''
          AND {where}
        """,
        params,
    ).fetchall()

    if not rows:
        return []

    counts: dict[str, int] = defaultdict(int)
    total = 0
    for row in rows:
        category = _classify_command(row["command"])
        counts[category] += 1
        total += 1

    result = []
    for category, count in sorted(counts.items(), key=lambda x: -x[1]):
        result.append(
            {
                "category": category,
                "command_count": count,
                "pct_of_total": (round(count / total * 100, 1) if total else 0.0),
            }
        )
    return result


def compute_by_repo(
    conn: sqlite3.Connection,
    *,
    username: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10,
) -> list[dict]:
    if _has_period_bounds(since, until):
        where, params = _build_daily_where(username, since, until)
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(s.repo_name, '(no repo)') AS repo_name,
                COUNT(DISTINCT d.session_id) AS session_count,
                COALESCE(SUM(d.native_total_output_tokens), 0) AS total_output_tokens
            FROM session_daily_effective d
            JOIN sessions s ON s.session_id = d.session_id
            WHERE {where}
            GROUP BY COALESCE(s.repo_name, '(no repo)')
            ORDER BY total_output_tokens DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        grand_total_row = conn.execute(
            f"""
            SELECT COALESCE(SUM(d.native_total_output_tokens), 0) AS total
            FROM session_daily_effective d
            JOIN sessions s ON s.session_id = d.session_id
            WHERE {where}
            """,
            params,
        ).fetchone()
    else:
        where, params = _build_where(username, since, until)
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(s.repo_name, '(no repo)') AS repo_name,
                COUNT(*) AS session_count,
                COALESCE(SUM(s.native_total_output_tokens), 0) AS total_output_tokens
            FROM sessions s
            WHERE {where}
            GROUP BY COALESCE(s.repo_name, '(no repo)')
            ORDER BY total_output_tokens DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        # Use all-sessions total as denominator, not just top-N.
        grand_total_row = conn.execute(
            f"""
            SELECT COALESCE(SUM(s.native_total_output_tokens), 0) AS total
            FROM sessions s
            WHERE {where}
            """,
            params,
        ).fetchone()
    grand_total = grand_total_row["total"]

    result = []
    for row in rows:
        result.append(
            {
                "repo_name": row["repo_name"],
                "session_count": row["session_count"],
                "total_output_tokens": row["total_output_tokens"],
                "pct_of_total": (
                    round(row["total_output_tokens"] / grand_total * 100, 1)
                    if grand_total
                    else 0.0
                ),
            }
        )
    return result


def compute_by_period(
    conn: sqlite3.Connection,
    *,
    username: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Monthly rollup from per-day usage (session_daily_effective).

    Tokens land on the UTC day their events happened. Bucketing whole
    sessions by first_timestamp instead would dump a weeks-long session onto
    its start month (that inflated Apr/May 2026 and starved Jun). Sessions
    not re-synced since session_daily_usage was introduced degrade to
    start-date attribution via the view. A session active in N months counts
    toward each month's session_count. Sums use native_* so Claude Code
    resume chains count their inherited history once.
    """
    where, params = _build_daily_where(username, since, until)

    overview = conn.execute(
        f"""
        SELECT MIN(d.day) AS first_date, MAX(d.day) AS last_date
        FROM session_daily_effective d
        JOIN sessions s ON s.session_id = d.session_id
        WHERE {where}
        """,
        params,
    ).fetchone()

    first_date = overview["first_date"]
    last_date = overview["last_date"]
    if not first_date or not last_date:
        return []

    try:
        first_dt = datetime.fromisoformat(first_date.replace("Z", "+00:00"))
        last_dt = datetime.fromisoformat(last_date.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return []

    span_days = (last_dt - first_dt).days
    if span_days <= 30:
        return []

    rows = conn.execute(
        f"""
        SELECT
            SUBSTR(d.day, 1, 7) AS month,
            COUNT(DISTINCT d.session_id) AS session_count,
            COALESCE(SUM(d.native_total_output_tokens), 0) AS total_output_tokens,
            COALESCE(SUM(d.native_total_input_tokens), 0) AS total_input_tokens,
            COALESCE(SUM(d.native_cached_input_tokens), 0) AS cached_input_tokens,
            COALESCE(SUM(d.native_cache_creation_input_tokens), 0) AS cache_creation_input_tokens
        FROM session_daily_effective d
        JOIN sessions s ON s.session_id = d.session_id
        WHERE {where}
        GROUP BY SUBSTR(d.day, 1, 7)
        ORDER BY month
        """,
        params,
    ).fetchall()

    today = datetime.now().date()

    def _range_date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value[:10])
        except (TypeError, ValueError):
            return None

    since_date = _range_date(since)
    until_date = _range_date(until)

    result = []
    for row in rows:
        month_str = row["month"]  # "YYYY-MM"
        try:
            year, month = int(month_str[:4]), int(month_str[5:7])
            month_start = date(year, month, 1)
            month_end = date(year, month, calendar.monthrange(year, month)[1])
            # Never project a current-month rate over days that have not
            # happened yet, even when --until is in the future.
            if month_start.year == today.year and month_start.month == today.month:
                month_end = min(month_end, today)
            included_start = max(month_start, since_date or month_start)
            included_end = min(month_end, until_date or month_end)
            cal_days = (included_end - included_start).days + 1
        except (ValueError, IndexError):
            cal_days = 30
        days = max(cal_days, 1)
        result.append(
            {
                "month": row["month"],
                "session_count": row["session_count"],
                "total_output_tokens": row["total_output_tokens"],
                "total_input_tokens": row["total_input_tokens"],
                "cached_input_tokens": row["cached_input_tokens"],
                "cache_creation_input_tokens": row["cache_creation_input_tokens"],
                "tokens_per_day": row["total_output_tokens"] // days,
            }
        )
    return result


def compute_stats(
    conn: sqlite3.Connection,
    *,
    username: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    kwargs = {"username": username, "since": since, "until": until}
    overview = compute_overview(conn, **kwargs)
    by_pattern = compute_by_pattern(conn, **kwargs)
    subagent_breakdown = compute_subagent_breakdown(conn, **kwargs)
    by_repo = compute_by_repo(conn, **kwargs)
    by_period = compute_by_period(conn, **kwargs)
    return {
        "overview": overview,
        "by_pattern": by_pattern,
        "subagent_breakdown": subagent_breakdown,
        "by_repo": by_repo,
        "by_period": by_period,
    }


def _fmt(n: int) -> str:
    return f"{n:,}"


def _pct(v: float) -> str:
    return f"{v:.1f}%"


def format_stats(data: dict) -> list[str]:
    lines: list[str] = []
    ov = data["overview"]

    lines.append("=== Overview ===")
    lines.append(f"  Sessions:       {_fmt(ov['total_sessions'])}")
    lines.append(
        f"  Date range:     {(ov['first_date'] or '?')[:10]}"
        f" to {(ov['last_date'] or '?')[:10]}"
    )
    lines.append(f"  Input tokens:   {_fmt(ov['total_input_tokens'])}")
    lines.append(f"  Output tokens:  {_fmt(ov['total_output_tokens'])}")
    lines.append(f"  Cached tokens:  {_fmt(ov['cached_input_tokens'])}")
    lines.append(f"  Cache writes:   {_fmt(ov.get('cache_creation_input_tokens', 0))}")
    lines.append(f"  Repos touched:  {_fmt(ov['repos_touched'])}")

    patterns = data["by_pattern"]
    if patterns:
        lines.append("")
        lines.append("=== By behavioral pattern ===")
        hdr = f"  {'Pattern':<22}{'Sessions':>10}{'Output':>14}{'%':>8}{'Avg/sess':>12}"
        lines.append(hdr)
        for p in patterns:
            if p["session_count"] == 0:
                continue
            lines.append(
                f"  {p['pattern']:<22}"
                f"{_fmt(p['session_count']):>10}"
                f"{_fmt(p['total_output_tokens']):>14}"
                f"{_pct(p['pct_of_total']):>8}"
                f"{_fmt(p['avg_output_per_session']):>12}"
            )

    breakdown = data["subagent_breakdown"]
    if breakdown:
        lines.append("")
        lines.append("=== Subagent command breakdown ===")
        hdr = f"  {'Category':<16}{'Commands':>10}{'%':>8}"
        lines.append(hdr)
        for b in breakdown:
            lines.append(
                f"  {b['category']:<16}"
                f"{_fmt(b['command_count']):>10}"
                f"{_pct(b['pct_of_total']):>8}"
            )

    repos = data["by_repo"]
    if repos:
        lines.append("")
        lines.append("=== By repo (top 10) ===")
        hdr = f"  {'Repo':<30}{'Sessions':>10}{'Output':>14}{'%':>8}"
        lines.append(hdr)
        for r in repos:
            name = r["repo_name"]
            if len(name) > 28:
                name = name[:25] + "..."
            lines.append(
                f"  {name:<30}"
                f"{_fmt(r['session_count']):>10}"
                f"{_fmt(r['total_output_tokens']):>14}"
                f"{_pct(r['pct_of_total']):>8}"
            )

    periods = data["by_period"]
    if periods:
        lines.append("")
        lines.append("=== By month (event-dated) ===")
        hdr = f"  {'Month':<10}{'Sessions':>10}{'Input':>16}{'Output':>14}{'Tok/day':>12}"
        lines.append(hdr)
        for p in periods:
            lines.append(
                f"  {p['month']:<10}"
                f"{_fmt(p['session_count']):>10}"
                f"{_fmt(p.get('total_input_tokens', 0)):>16}"
                f"{_fmt(p['total_output_tokens']):>14}"
                f"{_fmt(p['tokens_per_day']):>12}"
            )

    return lines
