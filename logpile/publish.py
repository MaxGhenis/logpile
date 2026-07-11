"""Local-first publish review helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import sqlite3
from typing import Pattern


@dataclass(frozen=True)
class PatternRule:
    category: str
    severity: str
    title: str
    regex: Pattern[str]


@dataclass
class PublishFinding:
    category: str
    severity: str
    title: str
    evidence: str
    source: str
    line_number: int | None = None


@dataclass
class PublishReview:
    session_id: str
    source: str
    current_visibility: str
    visibility_source: str
    recommendation: str
    rationale: str
    inspected_path: str | None
    inspected_bytes: bytes | None = None
    metadata: dict[str, str | int | None] = field(default_factory=dict)
    findings: list[PublishFinding] = field(default_factory=list)


@dataclass
class PublishCandidate:
    session_id: str
    source: str
    username: str
    display_name: str
    session_origin: str | None
    project: str | None
    repo_name: str | None
    visibility: str
    first_timestamp: str | None
    last_timestamp: str | None
    session_status: str | None
    session_goal: str | None
    session_summary: str | None
    session_outcome: str | None
    review_recommendation: str | None = None
    review_rationale: str | None = None
    finding_count: int = 0
    high_findings: int = 0
    medium_findings: int = 0


_PRIVATE_KEY_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Private key material",
    regex=re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
)
_TOKEN_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Token or credential",
    regex=re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-(?:proj-)?[A-Za-z0-9]{20,}"
        r"|sk-ant-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16})\b"
    ),
)
_CREDENTIAL_ASSIGNMENT_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Secret-like assignment",
    regex=re.compile(
        r"(?i)\b(?:token|secret|password|passwd|api[_-]?key|access[_-]?key)\b[^\n]{0,40}"
        r"\b[A-Za-z0-9._\-/+=]{12,}\b"
    ),
)
_BEARER_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Bearer credential",
    regex=re.compile(
        r"(?i)\bbearer\b[^\n]{0,20}\b[A-Za-z0-9._\-/+=]{20,}\b"
    ),
)
_EMAIL_RULE = PatternRule(
    category="pii",
    severity="medium",
    title="Email address",
    regex=re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
)
_HOME_PATH_RULE = PatternRule(
    category="pii",
    severity="medium",
    title="Absolute home path",
    regex=re.compile(
        r"(?<!\w)(?:/Users/[^/\s\"'`]+(?:/[^\s\"'`]+)*|/home/[^/\s\"'`]+(?:/[^\s\"'`]+)*|~\/[^\s\"'`]+(?:/[^\s\"'`]+)*)"
    ),
)

_PATTERN_RULES = (
    _PRIVATE_KEY_RULE,
    _TOKEN_RULE,
    _CREDENTIAL_ASSIGNMENT_RULE,
    _BEARER_RULE,
    _EMAIL_RULE,
    _HOME_PATH_RULE,
)

_VISIBILITY_ORDER = {"private": 0, "unlisted": 1, "public": 2}


def _needs_visibility_tightening(current_visibility: str, recommendation: str | None) -> bool:
    if recommendation not in _VISIBILITY_ORDER:
        return False
    current_rank = _VISIBILITY_ORDER.get(current_visibility, max(_VISIBILITY_ORDER.values()))
    recommended_rank = _VISIBILITY_ORDER[recommendation]
    return recommended_rank < current_rank


def _clip(value: str, limit: int = 180) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _normalize_path(value: str | None) -> Path | None:
    if not value:
        return None
    try:
        path = Path(value).expanduser()
        if path.exists():
            try:
                return path.resolve()
            except OSError:
                return path
        return path
    except OSError:
        return None


def _load_session_row(conn: sqlite3.Connection, session_id_prefix: str):
    exact = conn.execute(
        """
        SELECT
            s.*,
            COALESCE(u.default_session_visibility, 'unlisted') AS default_session_visibility,
            COALESCE(u.profile_visibility, 'public') AS user_profile_visibility,
            COALESCE(u.display_name, u.username, s.username) AS display_name
        FROM sessions s
        LEFT JOIN users u ON u.username = s.username
        WHERE s.session_id = ?
        LIMIT 1
        """,
        (session_id_prefix,),
    ).fetchone()
    if exact:
        return exact

    rows = conn.execute(
        """
        SELECT
            s.*,
            COALESCE(u.default_session_visibility, 'unlisted') AS default_session_visibility,
            COALESCE(u.profile_visibility, 'public') AS user_profile_visibility,
            COALESCE(u.display_name, u.username, s.username) AS display_name
        FROM sessions s
        LEFT JOIN users u ON u.username = s.username
        WHERE s.session_id LIKE ?
        ORDER BY LENGTH(s.session_id), s.session_id
        LIMIT 2
        """,
        (f"{session_id_prefix}%",),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise ValueError(
            f"Ambiguous session id prefix '{session_id_prefix}'. Use a longer session id."
        )
    return rows[0]


def _inspect_path(row) -> Path | None:
    # Prefer the artifact that would actually be published. Fall back to the
    # source file only when no shared copy exists yet.
    candidates = [
        row["shared_path"] if row["shared_path"] else None,
        row["source_path"] if row["source_path"] else None,
    ]
    for candidate in candidates:
        path = _normalize_path(candidate)
        if path:
            return path
    return None


def _scan_text(text: str, source: str, findings: list[PublishFinding]) -> None:
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule in _PATTERN_RULES:
            if rule.regex.search(line):
                findings.append(
                    PublishFinding(
                        category=rule.category,
                        severity=rule.severity,
                        title=rule.title,
                        evidence=_clip(line),
                        source=source,
                        line_number=line_number,
                    )
                )


def _scan_metadata(row, findings: list[PublishFinding]) -> None:
    metadata_fields = (
        "repo_name",
        "project",
        "first_user_message",
        "machine",
        "model",
    )
    for field_name in metadata_fields:
        value = row[field_name]
        if not value:
            continue
        text = f"{field_name}: {value}"
        _scan_text(text, f"metadata.{field_name}", findings)


def _dedupe_findings(findings: list[PublishFinding]) -> list[PublishFinding]:
    seen: set[tuple[str, str, str, str, int | None]] = set()
    unique: list[PublishFinding] = []
    for finding in findings:
        key = (
            finding.category,
            finding.severity,
            finding.title,
            finding.evidence,
            finding.line_number,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def _recommendation(findings: list[PublishFinding]) -> tuple[str, str]:
    if any(f.severity == "high" for f in findings):
        return "private", "High-risk secrets or credentials were detected."
    if any(f.severity == "medium" for f in findings):
        return "unlisted", "Sensitive-but-not-secret content was detected."
    return "public", "No obvious risky content was detected."


def review_publish_session(
    conn: sqlite3.Connection,
    session_id_prefix: str,
) -> PublishReview | None:
    row = _load_session_row(conn, session_id_prefix)
    if not row:
        return None

    inspected_path = _inspect_path(row)
    inspected_bytes: bytes | None = None
    findings: list[PublishFinding] = []
    if inspected_path and inspected_path.exists():
        inspected_bytes = inspected_path.read_bytes()
        _scan_text(
            inspected_bytes.decode("utf-8", errors="replace"),
            str(inspected_path),
            findings,
        )
    else:
        findings.append(
            PublishFinding(
                category="missing",
                severity="high",
                title="Session file missing",
                evidence=f"Could not find shared/source session file for {row['session_id']}",
                source="metadata",
                line_number=None,
            )
        )

    _scan_metadata(row, findings)
    findings = sorted(
        _dedupe_findings(findings),
        key=lambda item: (
            0 if item.severity == "high" else 1 if item.severity == "medium" else 2,
            item.source,
            item.line_number or 0,
            item.title,
        ),
    )
    recommendation, rationale = _recommendation(findings)
    return PublishReview(
        session_id=row["session_id"],
        source=row["source"],
        current_visibility=row["visibility"],
        visibility_source=row["visibility_source"],
        recommendation=recommendation,
        rationale=rationale,
        inspected_path=str(inspected_path) if inspected_path else None,
        inspected_bytes=inspected_bytes,
        metadata={
            "display_name": row["display_name"],
            "username": row["username"],
            "default_session_visibility": row["default_session_visibility"],
            "user_profile_visibility": row["user_profile_visibility"],
            "project": row["project"],
            "repo_name": row["repo_name"],
            "git_branch": row["git_branch"],
            "git_commit": row["git_commit"],
            "visibility": row["visibility"],
            "visibility_source": row["visibility_source"],
            "source_path": row["source_path"],
            "shared_path": row["shared_path"],
            "workspace_root": row["workspace_root"],
            "worktree_root": row["worktree_root"],
            "repo_root": row["repo_root"],
            "model": row["model"],
        },
        findings=findings,
    )


def preserve_reviewed_artifact(
    conn: sqlite3.Connection,
    review: PublishReview,
) -> None:
    if review.inspected_bytes is None:
        return
    row = conn.execute(
        "SELECT shared_path, visibility FROM sessions WHERE session_id = ?",
        (review.session_id,),
    ).fetchone()
    if not row or row["visibility"] == "private":
        return
    shared_path = _normalize_path(row["shared_path"])
    if not shared_path:
        return
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_bytes(review.inspected_bytes)


def serialize_publish_review(review: PublishReview) -> dict:
    return {
        "session_id": review.session_id,
        "source": review.source,
        "current_visibility": review.current_visibility,
        "visibility_source": review.visibility_source,
        "recommendation": review.recommendation,
        "rationale": review.rationale,
        "inspected_path": review.inspected_path,
        "metadata": dict(review.metadata),
        "findings": [
            {
                "category": finding.category,
                "severity": finding.severity,
                "title": finding.title,
                "evidence": finding.evidence,
                "source": finding.source,
                "line_number": finding.line_number,
            }
            for finding in review.findings
        ],
    }


def list_publish_candidates(
    conn: sqlite3.Connection,
    *,
    user_identifier: str | None = None,
    visibility: str = "pending",
    status: str | None = None,
    origin: str | None = None,
    limit: int = 25,
    include_reviews: bool = False,
) -> list[PublishCandidate]:
    where_sql, params = _publish_queue_filter_sql(
        user_identifier=user_identifier,
        visibility=visibility,
        status=status,
        origin=origin,
    )
    normalized_visibility = (visibility or "pending").strip().lower()
    fetch_limit = None if normalized_visibility == "needs_changes" else max(1, min(limit, 200))

    query = f"""
        SELECT
            s.session_id,
            s.source,
            s.username,
            COALESCE(u.display_name, s.username) AS display_name,
            s.session_origin,
            s.project,
            s.repo_name,
            s.visibility,
            s.first_timestamp,
            s.last_timestamp,
            s.session_status,
            s.session_goal,
            s.session_summary,
            s.session_outcome
        FROM sessions s
        LEFT JOIN users u ON u.username = s.username
        WHERE {where_sql}
        ORDER BY
            CASE s.visibility WHEN 'unlisted' THEN 0 WHEN 'private' THEN 1 ELSE 2 END,
            s.first_timestamp DESC,
            s.session_id DESC
    """
    if fetch_limit is not None:
        query += "\n        LIMIT ?"
        rows = conn.execute(query, params + [fetch_limit]).fetchall()
    else:
        rows = conn.execute(query, params).fetchall()

    candidates: list[PublishCandidate] = []
    for row in rows:
        candidate = PublishCandidate(
            session_id=row["session_id"],
            source=row["source"],
            username=row["username"],
            display_name=row["display_name"],
            session_origin=row["session_origin"],
            project=row["project"],
            repo_name=row["repo_name"],
            visibility=row["visibility"],
            first_timestamp=row["first_timestamp"],
            last_timestamp=row["last_timestamp"],
            session_status=row["session_status"],
            session_goal=row["session_goal"],
            session_summary=row["session_summary"],
            session_outcome=row["session_outcome"],
        )
        if include_reviews or normalized_visibility == "needs_changes":
            review = review_publish_session(conn, candidate.session_id)
            if review:
                candidate.review_recommendation = review.recommendation
                candidate.review_rationale = review.rationale
                candidate.finding_count = len(review.findings)
                candidate.high_findings = sum(1 for finding in review.findings if finding.severity == "high")
                candidate.medium_findings = sum(1 for finding in review.findings if finding.severity == "medium")
                if normalized_visibility == "needs_changes" and not _needs_visibility_tightening(
                    candidate.visibility,
                    review.recommendation,
                ):
                    continue
        elif normalized_visibility == "needs_changes":
            continue
        candidates.append(candidate)
        if normalized_visibility == "needs_changes" and len(candidates) >= max(1, min(limit, 200)):
            break
    return candidates


def count_publish_candidates(
    conn: sqlite3.Connection,
    *,
    user_identifier: str | None = None,
    visibility: str = "pending",
    status: str | None = None,
    origin: str | None = None,
) -> int:
    where_sql, params = _publish_queue_filter_sql(
        user_identifier=user_identifier,
        visibility=visibility,
        status=status,
        origin=origin,
    )
    normalized_visibility = (visibility or "pending").strip().lower()
    if normalized_visibility == "needs_changes":
        rows = conn.execute(
            f"""
            SELECT session_id, visibility
            FROM sessions s
            WHERE {where_sql}
            ORDER BY
                CASE s.visibility WHEN 'unlisted' THEN 0 WHEN 'private' THEN 1 ELSE 2 END,
                s.first_timestamp DESC,
                s.session_id DESC
            """,
            params,
        ).fetchall()
        count = 0
        for row in rows:
            review = review_publish_session(conn, row["session_id"])
            if review and _needs_visibility_tightening(row["visibility"], review.recommendation):
                count += 1
        return count
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM sessions s WHERE {where_sql}",
        params,
    ).fetchone()
    return int(row["count"]) if row else 0


def _publish_queue_filter_sql(
    *,
    user_identifier: str | None = None,
    visibility: str = "pending",
    status: str | None = None,
    origin: str | None = None,
) -> tuple[str, list[object]]:
    clauses = ["1 = 1"]
    params: list[object] = []

    if user_identifier:
        clauses.append("s.username = ?")
        params.append(user_identifier)

    normalized_visibility = (visibility or "pending").strip().lower()
    if normalized_visibility == "pending":
        clauses.append("s.visibility IN ('private', 'unlisted')")
    elif normalized_visibility == "needs_changes":
        clauses.append("s.visibility IN ('public', 'unlisted')")
    elif normalized_visibility in {"private", "unlisted", "public"}:
        clauses.append("s.visibility = ?")
        params.append(normalized_visibility)
    elif normalized_visibility != "all":
        raise ValueError(f"Unsupported publish queue visibility filter: {visibility}")

    normalized_status = (status or "").strip().lower()
    if normalized_status:
        if normalized_status not in {"exploration", "success", "partial", "failed"}:
            raise ValueError(f"Unsupported publish queue status filter: {status}")
        clauses.append("COALESCE(s.session_status, 'exploration') = ?")
        params.append(normalized_status)

    normalized_origin = (origin or "").strip().lower()
    if normalized_origin:
        if normalized_origin not in {
            "human_direct",
            "human_delegated",
            "system_generated",
            "pipeline_eval",
            "meta_scaffolding",
        }:
            raise ValueError(f"Unsupported publish queue origin filter: {origin}")
        clauses.append("COALESCE(s.session_origin, 'human_direct') = ?")
        params.append(normalized_origin)

    return " AND ".join(clauses), params


def serialize_publish_candidate(candidate: PublishCandidate) -> dict:
    return {
        "session_id": candidate.session_id,
        "source": candidate.source,
        "username": candidate.username,
        "display_name": candidate.display_name,
        "session_origin": candidate.session_origin,
        "project": candidate.project,
        "repo_name": candidate.repo_name,
        "visibility": candidate.visibility,
        "first_timestamp": candidate.first_timestamp,
        "last_timestamp": candidate.last_timestamp,
        "session_status": candidate.session_status,
        "session_goal": candidate.session_goal,
        "session_summary": candidate.session_summary,
        "session_outcome": candidate.session_outcome,
        "review_recommendation": candidate.review_recommendation,
        "review_rationale": candidate.review_rationale,
        "needs_visibility_change": _needs_visibility_tightening(
            candidate.visibility,
            candidate.review_recommendation,
        ),
        "finding_count": candidate.finding_count,
        "high_findings": candidate.high_findings,
        "medium_findings": candidate.medium_findings,
    }


def format_publish_review(review: PublishReview) -> list[str]:
    lines = [
        f"Session: {review.session_id}",
        f"Source: {review.source}",
        f"Current visibility: {review.current_visibility} ({review.visibility_source})",
        f"Inspected file: {review.inspected_path or 'missing'}",
        f"Recommendation: {review.recommendation}",
        f"Rationale: {review.rationale}",
        "Metadata:",
    ]
    metadata_order = (
        "display_name",
        "username",
        "project",
        "repo_name",
        "git_branch",
        "git_commit",
        "model",
    )
    for key in metadata_order:
        value = review.metadata.get(key)
        if value:
            lines.append(f"  {key}: {value}")
    path_order = (
        "source_path",
        "shared_path",
        "workspace_root",
        "worktree_root",
        "repo_root",
    )
    for key in path_order:
        value = review.metadata.get(key)
        if value:
            lines.append(f"  {key}: {value}")

    lines.append(f"Findings: {len(review.findings)}")
    if not review.findings:
        lines.append("  none")
        return lines

    for finding in review.findings:
        location = finding.source
        if finding.line_number is not None:
            location = f"{location}:{finding.line_number}"
        lines.append(
            f"- [{finding.severity}] {finding.category}: {finding.title} "
            f"({location})"
        )
        lines.append(f"  {finding.evidence}")
    return lines


def format_publish_queue(candidates: list[PublishCandidate]) -> list[str]:
    if not candidates:
        return ["No publish candidates matched the current filters."]

    lines: list[str] = []
    for candidate in candidates:
        scope = candidate.repo_name or candidate.project or "unknown"
        status = candidate.session_status or "exploration"
        lines.append(
            f"{candidate.session_id}\t{candidate.visibility}\t{status}\t{scope}"
        )
        if candidate.session_goal:
            lines.append(f"  goal: {candidate.session_goal}")
        if candidate.session_summary:
            lines.append(f"  summary: {candidate.session_summary}")
        if candidate.session_outcome:
            lines.append(f"  outcome: {candidate.session_outcome}")
        if candidate.review_recommendation:
            lines.append(
                f"  review: {candidate.review_recommendation} "
                f"({candidate.finding_count} findings; "
                f"{candidate.high_findings} high, {candidate.medium_findings} medium)"
            )
    return lines


def can_apply_visibility(review: PublishReview, target_visibility: str, force: bool = False) -> tuple[bool, str]:
    target_rank = _VISIBILITY_ORDER[target_visibility]
    recommendation_rank = _VISIBILITY_ORDER[review.recommendation]
    if target_rank <= recommendation_rank:
        return True, ""
    if force:
        return True, ""
    return (
        False,
        f"Review recommends {review.recommendation}; refusing to set {target_visibility} without --force.",
    )
