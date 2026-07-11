from __future__ import annotations

import json
import sqlite3

from logpile.db import migrate_db
from logpile.stats import (
    classify_session,
    compute_by_pattern,
    compute_by_period,
    compute_by_repo,
    compute_overview,
    compute_stats,
    compute_subagent_breakdown,
    format_stats,
)


# ── fixtures ──────────────────────────────────────────────────────────


def _make_db() -> sqlite3.Connection:
    # Real schema (including session_daily_usage and the
    # session_daily_effective view) so stats queries run against what
    # production runs against.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate_db(conn)
    return conn


def _insert_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    source: str = "claudecode",
    username: str = "alice",
    repo_name: str | None = "my-repo",
    spawn_depth: int = 0,
    parent_session_id: str | None = None,
    source_path: str | None = None,
    user_message_count: int = 1,
    tool_call_count: int = 0,
    total_input_tokens: int = 1000,
    total_output_tokens: int = 500,
    cached_input_tokens: int = 200,
    first_timestamp: str = "2026-03-15T10:00:00Z",
) -> None:
    # native_* mirrors the transcript columns, as refresh_native_usage
    # leaves any ledger without resume-chain duplication.
    conn.execute(
        """
        INSERT INTO sessions (
            session_id, source, username, repo_name,
            spawn_depth, parent_session_id,
            user_message_count, tool_call_count,
            total_input_tokens, total_output_tokens, cached_input_tokens,
            native_total_input_tokens, native_total_output_tokens,
            native_cached_input_tokens,
            first_timestamp, last_timestamp, source_path, shared_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            source,
            username,
            repo_name,
            spawn_depth,
            parent_session_id,
            user_message_count,
            tool_call_count,
            total_input_tokens,
            total_output_tokens,
            cached_input_tokens,
            total_input_tokens,
            total_output_tokens,
            cached_input_tokens,
            first_timestamp,
            first_timestamp,
            source_path or f"/tmp/{session_id}.jsonl",
            "",
        ),
    )


def _insert_daily(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    day: str,
    total_input_tokens: int = 0,
    total_output_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    user_message_count: int = 0,
    assistant_message_count: int = 0,
    tool_call_count: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO session_daily_usage (
            session_id, day, total_input_tokens, total_output_tokens,
            cached_input_tokens, cache_creation_input_tokens,
            native_total_input_tokens, native_total_output_tokens,
            native_cached_input_tokens, native_cache_creation_input_tokens,
            user_message_count, assistant_message_count, tool_call_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            day,
            total_input_tokens,
            total_output_tokens,
            cached_input_tokens,
            cache_creation_input_tokens,
            total_input_tokens,
            total_output_tokens,
            cached_input_tokens,
            cache_creation_input_tokens,
            user_message_count,
            assistant_message_count,
            tool_call_count,
        ),
    )


def _insert_tool_call(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    command: str,
    tool_name: str = "Bash",
    timestamp: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO tool_calls (session_id, tool_name, command, timestamp) "
        "VALUES (?, ?, ?, ?)",
        (session_id, tool_name, command, timestamp),
    )


# ── classify_session ──────────────────────────────────────────────────


class TestClassifySession:
    def test_subagent(self):
        assert classify_session(1, 5, 10) == "subagent"
        assert classify_session(3, 200, 1000) == "subagent"

    def test_marathon(self):
        assert classify_session(0, 101, 0) == "marathon"
        assert classify_session(0, 500, 1000) == "marathon"

    def test_heavy_tooling(self):
        assert classify_session(0, 10, 501) == "heavy-tooling"
        assert classify_session(0, 100, 600) == "heavy-tooling"

    def test_long_conversation(self):
        assert classify_session(0, 20, 500) == "long-conversation"
        assert classify_session(0, 100, 500) == "long-conversation"
        assert classify_session(0, 50, 100) == "long-conversation"

    def test_medium_conversation(self):
        assert classify_session(0, 5, 100) == "medium-conversation"
        assert classify_session(0, 19, 100) == "medium-conversation"

    def test_short_task(self):
        assert classify_session(0, 1, 0) == "short-task"
        assert classify_session(0, 4, 499) == "short-task"
        assert classify_session(0, 0, 0) == "short-task"

    def test_boundary_marathon_beats_heavy_tooling(self):
        # user_message_count > 100 triggers marathon before heavy-tooling
        assert classify_session(0, 101, 501) == "marathon"

    def test_boundary_subagent_beats_everything(self):
        assert classify_session(1, 200, 1000) == "subagent"
        assert (
            classify_session(0, 200, 1000, subagent_evidence=True)
            == "subagent"
        )

    def test_boundary_long_vs_medium(self):
        assert classify_session(0, 19, 400) == "medium-conversation"
        assert classify_session(0, 20, 400) == "long-conversation"


# ── compute_overview ──────────────────────────────────────────────────


class TestComputeOverview:
    def test_empty_db(self):
        conn = _make_db()
        result = compute_overview(conn)
        assert result["total_sessions"] == 0
        assert result["total_input_tokens"] == 0
        assert result["total_output_tokens"] == 0

    def test_basic_aggregation(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="s1",
            total_input_tokens=1000,
            total_output_tokens=500,
            cached_input_tokens=200,
            repo_name="repo-a",
            first_timestamp="2026-03-01T10:00:00Z",
        )
        _insert_session(
            conn,
            session_id="s2",
            total_input_tokens=2000,
            total_output_tokens=800,
            cached_input_tokens=300,
            repo_name="repo-b",
            first_timestamp="2026-03-02T10:00:00Z",
        )
        result = compute_overview(conn)
        assert result["total_sessions"] == 2
        assert result["total_input_tokens"] == 3000
        assert result["total_output_tokens"] == 1300
        assert result["cached_input_tokens"] == 500
        assert result["repos_touched"] == 2
        assert result["first_date"] == "2026-03-01T10:00:00Z"
        assert result["last_date"] == "2026-03-02T10:00:00Z"

    def test_filter_by_username(self):
        conn = _make_db()
        _insert_session(conn, session_id="s1", username="alice")
        _insert_session(conn, session_id="s2", username="bob")
        result = compute_overview(conn, username="alice")
        assert result["total_sessions"] == 1

    def test_filter_by_since(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="s1",
            first_timestamp="2026-01-01T10:00:00Z",
        )
        _insert_session(
            conn,
            session_id="s2",
            first_timestamp="2026-04-01T10:00:00Z",
        )
        result = compute_overview(conn, since="2026-03-01")
        assert result["total_sessions"] == 1

    def test_filter_by_until(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="s1",
            first_timestamp="2026-01-01T10:00:00Z",
        )
        _insert_session(
            conn,
            session_id="s2",
            first_timestamp="2026-04-01T10:00:00Z",
        )
        result = compute_overview(conn, until="2026-02-01")
        assert result["total_sessions"] == 1


# ── compute_by_pattern ────────────────────────────────────────────────


class TestComputeByPattern:
    def test_classifies_and_aggregates(self):
        conn = _make_db()
        # short-task
        _insert_session(
            conn,
            session_id="s1",
            spawn_depth=0,
            user_message_count=2,
            tool_call_count=5,
            total_output_tokens=100,
        )
        # subagent
        _insert_session(
            conn,
            session_id="s2",
            spawn_depth=1,
            user_message_count=3,
            tool_call_count=10,
            total_output_tokens=400,
        )
        result = compute_by_pattern(conn)
        by_name = {p["pattern"]: p for p in result}

        assert by_name["short-task"]["session_count"] == 1
        assert by_name["short-task"]["total_output_tokens"] == 100
        assert by_name["short-task"]["avg_output_per_session"] == 100

        assert by_name["subagent"]["session_count"] == 1
        assert by_name["subagent"]["total_output_tokens"] == 400
        assert by_name["subagent"]["pct_of_total"] == 80.0

    def test_empty_patterns_have_zero(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="s1",
            spawn_depth=0,
            user_message_count=1,
            tool_call_count=0,
            total_output_tokens=100,
        )
        result = compute_by_pattern(conn)
        by_name = {p["pattern"]: p for p in result}
        assert by_name["marathon"]["session_count"] == 0
        assert by_name["marathon"]["avg_output_per_session"] == 0

    def test_percentages_sum_to_100(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="s1",
            spawn_depth=0,
            user_message_count=2,
            total_output_tokens=300,
        )
        _insert_session(
            conn,
            session_id="s2",
            spawn_depth=1,
            total_output_tokens=700,
        )
        result = compute_by_pattern(conn)
        total_pct = sum(p["pct_of_total"] for p in result)
        assert abs(total_pct - 100.0) < 0.2

    def test_claude_parent_and_subagent_path_are_defensive_evidence(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="parent-evidence",
            spawn_depth=0,
            parent_session_id="root-session",
            source_path="/tmp/demo/parent-evidence.jsonl",
            total_output_tokens=100,
        )
        _insert_session(
            conn,
            session_id="path-evidence",
            spawn_depth=0,
            source_path=(
                "/tmp/.claude/projects/-tmp-demo/root-session/"
                "subagents/agent-worker.jsonl"
            ),
            total_output_tokens=200,
        )

        by_name = {row["pattern"]: row for row in compute_by_pattern(conn)}
        assert by_name["subagent"]["session_count"] == 2
        assert by_name["subagent"]["total_output_tokens"] == 300
        assert by_name["short-task"]["session_count"] == 0


# ── compute_subagent_breakdown ────────────────────────────────────────


class TestComputeSubagentBreakdown:
    def test_empty_when_no_subagents(self):
        conn = _make_db()
        _insert_session(conn, session_id="s1", spawn_depth=0)
        result = compute_subagent_breakdown(conn)
        assert result == []

    def test_categorizes_commands(self):
        conn = _make_db()
        _insert_session(conn, session_id="s1", spawn_depth=1)
        _insert_tool_call(conn, session_id="s1", command="gh pr list")
        _insert_tool_call(conn, session_id="s1", command="gh pr merge 42")
        _insert_tool_call(conn, session_id="s1", command="pytest tests/")
        _insert_tool_call(conn, session_id="s1", command="rg pattern src/")
        _insert_tool_call(conn, session_id="s1", command="something unknown")

        result = compute_subagent_breakdown(conn)
        by_cat = {r["category"]: r for r in result}

        assert by_cat["pr-ops"]["command_count"] == 2
        assert by_cat["testing"]["command_count"] == 1
        assert by_cat["search"]["command_count"] == 1
        assert by_cat["other"]["command_count"] == 1

    def test_ignores_non_subagent_sessions(self):
        conn = _make_db()
        _insert_session(conn, session_id="s1", spawn_depth=0)
        _insert_tool_call(conn, session_id="s1", command="gh pr list")
        _insert_session(conn, session_id="s2", spawn_depth=1)
        _insert_tool_call(conn, session_id="s2", command="git status")

        result = compute_subagent_breakdown(conn)
        by_cat = {r["category"]: r for r in result}
        assert "pr-ops" not in by_cat
        assert by_cat["git"]["command_count"] == 1

    def test_percentages(self):
        conn = _make_db()
        _insert_session(conn, session_id="s1", spawn_depth=1)
        _insert_tool_call(conn, session_id="s1", command="git status")
        _insert_tool_call(conn, session_id="s1", command="git diff")
        _insert_tool_call(conn, session_id="s1", command="git log")
        _insert_tool_call(conn, session_id="s1", command="pytest test.py")

        result = compute_subagent_breakdown(conn)
        by_cat = {r["category"]: r for r in result}
        assert by_cat["git"]["pct_of_total"] == 75.0
        assert by_cat["testing"]["pct_of_total"] == 25.0

    def test_bounded_rows_use_each_tool_timestamp_in_utc(self):
        conn = _make_db()
        # This is the minimized shape of a real Claude sidechain path.  Its
        # stale depth and pre-period session start must not suppress tools
        # that actually ran in the requested UTC day.
        _insert_session(
            conn,
            session_id="agent-worker",
            spawn_depth=0,
            source_path=(
                "/tmp/.claude/projects/-tmp-demo/root-session/"
                "subagents/agent-worker.jsonl"
            ),
            first_timestamp="2026-06-01T10:00:00Z",
        )
        _insert_tool_call(
            conn,
            session_id="agent-worker",
            command="git status",
            timestamp="2026-07-01T23:30:00-02:00",  # July 2 UTC
        )
        _insert_tool_call(
            conn,
            session_id="agent-worker",
            command="pytest tests/",
            timestamp="2026-07-03T00:30:00+02:00",  # July 2 UTC
        )
        _insert_tool_call(
            conn,
            session_id="agent-worker",
            command="gh pr list",
            timestamp="2026-07-02T23:30:00-02:00",  # July 3 UTC
        )
        _insert_tool_call(
            conn,
            session_id="agent-worker",
            command="rg missing timestamp",
        )
        _insert_tool_call(
            conn,
            session_id="agent-worker",
            command="python malformed.py",
            timestamp="not-a-timestamp",
        )

        result = compute_subagent_breakdown(
            conn,
            since="2026-07-02",
            until="2026-07-02",
        )
        by_cat = {row["category"]: row for row in result}
        assert set(by_cat) == {"git", "testing"}
        assert by_cat["git"]["command_count"] == 1
        assert by_cat["testing"]["command_count"] == 1


# ── compute_by_repo ───────────────────────────────────────────────────


class TestComputeByRepo:
    def test_groups_by_repo(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="s1",
            repo_name="repo-a",
            total_output_tokens=500,
        )
        _insert_session(
            conn,
            session_id="s2",
            repo_name="repo-a",
            total_output_tokens=300,
        )
        _insert_session(
            conn,
            session_id="s3",
            repo_name="repo-b",
            total_output_tokens=200,
        )

        result = compute_by_repo(conn)
        assert result[0]["repo_name"] == "repo-a"
        assert result[0]["session_count"] == 2
        assert result[0]["total_output_tokens"] == 800
        assert result[1]["repo_name"] == "repo-b"

    def test_null_repo_grouped(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="s1",
            repo_name=None,
            total_output_tokens=100,
        )
        result = compute_by_repo(conn)
        assert result[0]["repo_name"] == "(no repo)"

    def test_limit(self):
        conn = _make_db()
        for i in range(15):
            _insert_session(
                conn,
                session_id=f"s{i}",
                repo_name=f"repo-{i}",
                total_output_tokens=100,
            )
        result = compute_by_repo(conn, limit=5)
        assert len(result) == 5

    def test_ordered_by_output_desc(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="s1",
            repo_name="small",
            total_output_tokens=10,
        )
        _insert_session(
            conn,
            session_id="s2",
            repo_name="big",
            total_output_tokens=9999,
        )
        result = compute_by_repo(conn)
        assert result[0]["repo_name"] == "big"


# ── bounded event-period semantics ───────────────────────────────────


class TestBoundedEventPeriodSemantics:
    def test_all_sections_use_daily_usage_and_count_active_sessions_once(self):
        conn = _make_db()

        # Started before the range, but active on two in-range days.
        _insert_session(
            conn,
            session_id="pre-range-active",
            repo_name="repo-a",
            first_timestamp="2026-06-01T10:00:00Z",
            total_input_tokens=10_000,
            total_output_tokens=1_000,
            cached_input_tokens=1_000,
        )
        _insert_daily(
            conn,
            session_id="pre-range-active",
            day="2026-06-30",
            total_input_tokens=100,
            total_output_tokens=10,
            cached_input_tokens=10,
        )
        _insert_daily(
            conn,
            session_id="pre-range-active",
            day="2026-07-05",
            total_input_tokens=1_000,
            total_output_tokens=100,
            cached_input_tokens=100,
            cache_creation_input_tokens=50,
        )
        _insert_daily(
            conn,
            session_id="pre-range-active",
            day="2026-07-06",
            total_input_tokens=2_000,
            total_output_tokens=200,
            cached_input_tokens=200,
            cache_creation_input_tokens=60,
        )
        _insert_daily(
            conn,
            session_id="pre-range-active",
            day="2026-07-20",
            total_input_tokens=6_900,
            total_output_tokens=690,
            cached_input_tokens=690,
        )

        # Started in range, but most of its lifetime usage happened after it.
        _insert_session(
            conn,
            session_id="starts-inside",
            repo_name="repo-b",
            spawn_depth=1,
            first_timestamp="2026-07-08T10:00:00Z",
            total_input_tokens=4_000,
            total_output_tokens=400,
            cached_input_tokens=400,
        )
        _insert_daily(
            conn,
            session_id="starts-inside",
            day="2026-07-08",
            total_input_tokens=400,
            total_output_tokens=40,
            cached_input_tokens=40,
            cache_creation_input_tokens=10,
        )
        _insert_daily(
            conn,
            session_id="starts-inside",
            day="2026-08-01",
            total_input_tokens=3_600,
            total_output_tokens=360,
            cached_input_tokens=360,
        )

        # No event in the requested range, so this session is not active.
        _insert_session(
            conn,
            session_id="outside-only",
            repo_name="repo-c",
            first_timestamp="2026-05-01T10:00:00Z",
            total_input_tokens=500,
            total_output_tokens=50,
        )
        _insert_daily(
            conn,
            session_id="outside-only",
            day="2026-06-15",
            total_input_tokens=500,
            total_output_tokens=50,
        )

        kwargs = {"since": "2026-07-01", "until": "2026-07-10"}
        overview = compute_overview(conn, **kwargs)
        patterns = compute_by_pattern(conn, **kwargs)
        repos = compute_by_repo(conn, **kwargs)
        by_pattern = {row["pattern"]: row for row in patterns}
        by_repo = {row["repo_name"]: row for row in repos}

        assert overview == {
            "total_sessions": 2,
            "first_date": "2026-07-05",
            "last_date": "2026-07-08",
            "total_input_tokens": 3_400,
            "total_output_tokens": 340,
            "cached_input_tokens": 340,
            "cache_creation_input_tokens": 120,
            "repos_touched": 2,
        }
        # The two days for repo-a still represent one active session.
        assert by_pattern["short-task"]["session_count"] == 1
        assert by_pattern["short-task"]["total_output_tokens"] == 300
        assert by_pattern["subagent"]["session_count"] == 1
        assert by_pattern["subagent"]["total_output_tokens"] == 40
        assert by_repo["repo-a"]["session_count"] == 1
        assert by_repo["repo-a"]["total_output_tokens"] == 300
        assert by_repo["repo-b"]["session_count"] == 1
        assert by_repo["repo-b"]["total_output_tokens"] == 40

        assert sum(row["session_count"] for row in patterns) == overview["total_sessions"]
        assert sum(row["total_output_tokens"] for row in patterns) == overview["total_output_tokens"]
        assert sum(row["session_count"] for row in repos) == overview["total_sessions"]
        assert sum(row["total_output_tokens"] for row in repos) == overview["total_output_tokens"]

        # Supplying no date bounds intentionally retains whole-session rows.
        unbounded = compute_overview(conn)
        assert unbounded["total_sessions"] == 3
        assert unbounded["total_input_tokens"] == 14_500
        assert unbounded["total_output_tokens"] == 1_450


# ── compute_by_period ─────────────────────────────────────────────────


class TestComputeByPeriod:
    def test_empty_when_short_span(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="s1",
            first_timestamp="2026-03-01T10:00:00Z",
        )
        _insert_session(
            conn,
            session_id="s2",
            first_timestamp="2026-03-15T10:00:00Z",
        )
        result = compute_by_period(conn)
        assert result == []

    def test_monthly_breakdown(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="s1",
            first_timestamp="2026-01-10T10:00:00Z",
            total_output_tokens=100,
        )
        _insert_session(
            conn,
            session_id="s2",
            first_timestamp="2026-01-20T10:00:00Z",
            total_output_tokens=200,
        )
        _insert_session(
            conn,
            session_id="s3",
            first_timestamp="2026-03-15T10:00:00Z",
            total_output_tokens=300,
        )
        result = compute_by_period(conn)
        assert len(result) == 2
        assert result[0]["month"] == "2026-01"
        assert result[0]["session_count"] == 2
        assert result[0]["total_output_tokens"] == 300
        assert result[1]["month"] == "2026-03"

    def test_empty_db_returns_empty(self):
        conn = _make_db()
        result = compute_by_period(conn)
        assert result == []

    def test_long_session_lands_on_event_months_not_start_month(self):
        # A session spanning Apr->Jun used to dump everything on April
        # (that direction-flipped the Apr-Jun 2026 dashboard rollup).
        conn = _make_db()
        _insert_session(
            conn,
            session_id="marathon",
            first_timestamp="2026-04-01T10:00:00Z",
            total_output_tokens=900,
        )
        _insert_daily(
            conn, session_id="marathon", day="2026-04-01", total_output_tokens=100
        )
        _insert_daily(
            conn, session_id="marathon", day="2026-06-20", total_output_tokens=800
        )
        result = compute_by_period(conn)
        by_month = {r["month"]: r for r in result}
        assert by_month["2026-04"]["total_output_tokens"] == 100
        assert by_month["2026-06"]["total_output_tokens"] == 800
        assert "2026-05" not in by_month

    def test_sessions_without_daily_rows_fall_back_to_start_month(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="with-daily",
            first_timestamp="2026-01-10T10:00:00Z",
            total_output_tokens=999,  # session total ignored when daily rows exist
        )
        _insert_daily(
            conn, session_id="with-daily", day="2026-03-05", total_output_tokens=250
        )
        _insert_session(
            conn,
            session_id="legacy",
            first_timestamp="2026-01-20T10:00:00Z",
            total_output_tokens=200,
        )
        result = compute_by_period(conn)
        by_month = {r["month"]: r for r in result}
        assert by_month["2026-01"]["total_output_tokens"] == 200
        assert by_month["2026-03"]["total_output_tokens"] == 250

    def test_cache_creation_rolls_up_per_month(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="s1",
            first_timestamp="2026-01-10T10:00:00Z",
        )
        _insert_daily(
            conn,
            session_id="s1",
            day="2026-01-10",
            total_input_tokens=5_000,
            cache_creation_input_tokens=4_000,
        )
        _insert_session(
            conn,
            session_id="s2",
            first_timestamp="2026-03-15T10:00:00Z",
        )
        _insert_daily(conn, session_id="s2", day="2026-03-15", total_output_tokens=10)
        result = compute_by_period(conn)
        by_month = {r["month"]: r for r in result}
        assert by_month["2026-01"]["cache_creation_input_tokens"] == 4_000
        assert by_month["2026-01"]["total_input_tokens"] == 5_000

    def test_tokens_per_day_uses_bounded_partial_month_days(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="january",
            first_timestamp="2026-01-15T10:00:00Z",
        )
        _insert_daily(
            conn,
            session_id="january",
            day="2026-01-15",
            total_output_tokens=1_700,
        )
        _insert_session(
            conn,
            session_id="march",
            first_timestamp="2026-03-10T10:00:00Z",
        )
        _insert_daily(
            conn,
            session_id="march",
            day="2026-03-10",
            total_output_tokens=1_000,
        )

        result = compute_by_period(
            conn,
            since="2026-01-15",
            until="2026-03-10",
        )
        by_month = {row["month"]: row for row in result}

        # Jan 15-31 is 17 included days; Mar 1-10 is 10.
        assert by_month["2026-01"]["tokens_per_day"] == 100
        assert by_month["2026-03"]["tokens_per_day"] == 100


# ── compute_stats (end-to-end) ────────────────────────────────────────


class TestComputeStats:
    def test_returns_all_sections(self):
        conn = _make_db()
        _insert_session(conn, session_id="s1")
        result = compute_stats(conn)
        assert "overview" in result
        assert "by_pattern" in result
        assert "subagent_breakdown" in result
        assert "by_repo" in result
        assert "by_period" in result

    def test_end_to_end_with_mixed_data(self):
        conn = _make_db()
        # A regular short task
        _insert_session(
            conn,
            session_id="s1",
            spawn_depth=0,
            user_message_count=2,
            tool_call_count=5,
            total_input_tokens=1000,
            total_output_tokens=500,
            repo_name="my-repo",
            first_timestamp="2026-01-05T10:00:00Z",
        )
        # A subagent
        _insert_session(
            conn,
            session_id="s2",
            spawn_depth=1,
            user_message_count=3,
            tool_call_count=10,
            total_input_tokens=2000,
            total_output_tokens=1000,
            repo_name="my-repo",
            first_timestamp="2026-03-20T10:00:00Z",
        )
        _insert_tool_call(conn, session_id="s2", command="git status")
        _insert_tool_call(conn, session_id="s2", command="pytest tests/")

        result = compute_stats(conn)

        assert result["overview"]["total_sessions"] == 2
        assert result["overview"]["total_output_tokens"] == 1500

        by_name = {p["pattern"]: p for p in result["by_pattern"]}
        assert by_name["short-task"]["session_count"] == 1
        assert by_name["subagent"]["session_count"] == 1

        assert len(result["subagent_breakdown"]) == 2

        assert result["by_repo"][0]["repo_name"] == "my-repo"
        assert result["by_repo"][0]["session_count"] == 2

    def test_filters_propagate(self):
        conn = _make_db()
        _insert_session(conn, session_id="s1", username="alice")
        _insert_session(conn, session_id="s2", username="bob")
        result = compute_stats(conn, username="bob")
        assert result["overview"]["total_sessions"] == 1

    def test_json_serializable(self):
        conn = _make_db()
        _insert_session(conn, session_id="s1")
        result = compute_stats(conn)
        serialized = json.dumps(result)
        assert isinstance(serialized, str)
        round_tripped = json.loads(serialized)
        assert round_tripped["overview"]["total_sessions"] == 1


# ── format_stats ──────────────────────────────────────────────────────


class TestFormatStats:
    def test_contains_overview_header(self):
        conn = _make_db()
        _insert_session(conn, session_id="s1", total_output_tokens=1234567)
        data = compute_stats(conn)
        lines = format_stats(data)
        text = "\n".join(lines)
        assert "=== Overview ===" in text
        assert "1,234,567" in text

    def test_pattern_section_present(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="s1",
            spawn_depth=0,
            user_message_count=2,
            total_output_tokens=100,
        )
        data = compute_stats(conn)
        lines = format_stats(data)
        text = "\n".join(lines)
        assert "=== By behavioral pattern ===" in text
        assert "short-task" in text

    def test_subagent_section_only_when_present(self):
        conn = _make_db()
        _insert_session(conn, session_id="s1", spawn_depth=0)
        data = compute_stats(conn)
        lines = format_stats(data)
        text = "\n".join(lines)
        assert "Subagent command breakdown" not in text

    def test_subagent_section_when_present(self):
        conn = _make_db()
        _insert_session(conn, session_id="s1", spawn_depth=1)
        _insert_tool_call(conn, session_id="s1", command="git status")
        data = compute_stats(conn)
        lines = format_stats(data)
        text = "\n".join(lines)
        assert "=== Subagent command breakdown ===" in text
        assert "git" in text

    def test_numbers_formatted_with_commas(self):
        conn = _make_db()
        _insert_session(
            conn,
            session_id="s1",
            total_input_tokens=1_234_567,
            total_output_tokens=9_876_543,
        )
        data = compute_stats(conn)
        lines = format_stats(data)
        text = "\n".join(lines)
        assert "1,234,567" in text
        assert "9,876,543" in text

    def test_empty_db_still_formats(self):
        conn = _make_db()
        data = compute_stats(conn)
        lines = format_stats(data)
        text = "\n".join(lines)
        assert "=== Overview ===" in text
        assert "Sessions:       0" in text
