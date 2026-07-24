"""GitHub activity sync — pulls contribution data via GraphQL, stores daily rollups."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta


class GitHubSyncError(Exception):
    """Raised when the GitHub sync fails for a resolvable reason."""


def _resolve_token() -> str:
    """Find a usable GitHub token: env var first, then `gh auth token`."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token.strip()

    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        token = result.stdout.strip()
        if token:
            return token
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    raise GitHubSyncError(
        "No GitHub token found. Set GITHUB_TOKEN or run `gh auth login`."
    )


def _graphql(token: str, query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query against GitHub's API."""
    import urllib.error
    import urllib.request

    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "logpile-github-sync",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise GitHubSyncError(f"GitHub API error: {exc.code} {exc.reason}") from exc

    if payload.get("errors"):
        msg = "; ".join(err.get("message", "?") for err in payload["errors"])
        raise GitHubSyncError(f"GitHub GraphQL error: {msg}")
    return payload.get("data", {})


def _fetch_year(token: str, github_user: str, start: datetime, end: datetime) -> list[dict]:
    """Fetch contribution calendar for a window <= 1 year."""
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
          totalPullRequestContributions
          totalPullRequestReviewContributions
          totalIssueContributions
          contributionCalendar {
            weeks {
              contributionDays { date contributionCount }
            }
          }
        }
      }
    }
    """
    data = _graphql(
        token,
        query,
        {
            "login": github_user,
            "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    user = data.get("user")
    if not user:
        raise GitHubSyncError(f"GitHub user not found: {github_user}")

    calendar = user["contributionsCollection"]["contributionCalendar"]
    return [
        {"date": day["date"], "contributions": day["contributionCount"]}
        for week in calendar["weeks"]
        for day in week["contributionDays"]
    ]


def _fetch_pr_counts_per_day(token: str, github_user: str, since: str) -> dict[str, int]:
    """Fetch PR-opened counts per day via search (more accurate than calendar)."""
    query = """
    query($q: String!, $cursor: String) {
      search(query: $q, type: ISSUE, first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { ... on PullRequest { createdAt } }
      }
    }
    """
    counts: dict[str, int] = {}
    cursor: str | None = None
    q = f"author:{github_user} is:pr created:>={since}"
    seen = 0
    while True:
        data = _graphql(token, query, {"q": q, "cursor": cursor})
        search = data.get("search") or {}
        for node in search.get("nodes") or []:
            created = (node or {}).get("createdAt")
            if not created:
                continue
            day = created[:10]
            counts[day] = counts.get(day, 0) + 1
            seen += 1
        page = search.get("pageInfo") or {}
        if not page.get("hasNextPage") or seen >= 1000:
            break
        cursor = page.get("endCursor")
    return counts


def sync_user_github(conn, *, username: str, github_user: str, since: datetime | None = None) -> dict:
    """Pull GitHub contributions for one user into `user_github_daily`. Returns sync stats."""
    token = _resolve_token()
    now = datetime.now(UTC)
    start = since or (now - timedelta(days=540))  # ~18 months default
    start = start.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    # GitHub GraphQL caps contributionsCollection at 1 year windows. Chunk.
    all_days: dict[str, int] = {}
    cursor_start = start
    while cursor_start < now:
        cursor_end = min(cursor_start + timedelta(days=364), now)
        days = _fetch_year(token, github_user, cursor_start, cursor_end)
        for d in days:
            all_days[d["date"]] = d["contributions"]
        cursor_start = cursor_end + timedelta(seconds=1)

    # Layer in PR counts per day (search API)
    pr_counts = _fetch_pr_counts_per_day(token, github_user, start.strftime("%Y-%m-%d"))

    synced_at = now.isoformat()
    rows_written = 0
    for day, contribs in sorted(all_days.items()):
        conn.execute(
            """
            INSERT INTO user_github_daily
              (username, day, contributions, commits, prs_opened, prs_reviewed, issues_opened, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username, day) DO UPDATE SET
              contributions = excluded.contributions,
              prs_opened = excluded.prs_opened,
              synced_at = excluded.synced_at
            """,
            (
                username,
                day,
                contribs,
                0,  # commits — not separated from contribs in calendar API; keep 0 for now
                pr_counts.get(day, 0),
                0,
                0,
                synced_at,
            ),
        )
        rows_written += 1

    return {
        "username": username,
        "github_user": github_user,
        "days_synced": rows_written,
        "total_contributions": sum(all_days.values()),
        "total_prs": sum(pr_counts.values()),
        "since": start.strftime("%Y-%m-%d"),
    }


def users_with_github(conn) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT username, github_username FROM users WHERE github_username IS NOT NULL AND github_username != ''"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]
