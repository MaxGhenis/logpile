"""Local-first publish review helpers."""

from __future__ import annotations

import base64
import binascii
import codecs
import errno
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from re import Pattern
from typing import TextIO


@dataclass(frozen=True)
class PatternRule:
    category: str
    severity: str
    title: str
    regex: Pattern[str]
    secret_group: str | int = 0
    validator: Callable[[re.Match[str]], bool] | None = None


@dataclass
class PublishFinding:
    category: str
    severity: str
    title: str
    evidence: str
    source: str
    line_number: int | None = None
    match_start: int | None = None
    match_end: int | None = None
    match_index: int = 1
    match_count: int = 1
    omitted_count: int = 0


@dataclass
class PublishReview:
    session_id: str
    source: str
    current_visibility: str
    visibility_source: str
    recommendation: str
    rationale: str
    inspected_path: str | None
    inspected_sha256: str | None = None
    inspected_size: int = 0
    staged_path: str | None = None
    metadata_sha256: str | None = None
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


def _base64url_json(value: str) -> dict | None:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding)
        parsed = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _valid_jwt(match: re.Match[str]) -> bool:
    parts = match.group("secret").split(".")
    if len(parts) != 3 or len(parts[2]) < 16:
        return False
    header = _base64url_json(parts[0])
    payload = _base64url_json(parts[1])
    return bool(header and payload is not None and header.get("alg"))


def _valid_basic_auth(match: re.Match[str]) -> bool:
    try:
        decoded = base64.b64decode(match.group("secret"), validate=True)
    except (binascii.Error, ValueError, TypeError):
        return False
    username, separator, password = decoded.partition(b":")
    return bool(separator and username and password)


def _valid_ssn(match: re.Match[str]) -> bool:
    area, group, serial = match.group("secret").split("-")
    return (
        area not in {"000", "666"}
        and not area.startswith("9")
        and group != "00"
        and serial != "0000"
    )


def _valid_luhn_card(match: re.Match[str]) -> bool:
    digits = re.sub(r"[^0-9]", "", match.group("secret"))
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _valid_international_phone(match: re.Match[str]) -> bool:
    digits = re.sub(r"[^0-9]", "", match.group("secret"))
    return 8 <= len(digits) <= 15 and len(set(digits)) > 2


def _valid_us_phone(match: re.Match[str]) -> bool:
    """Validate a US/NANP candidate before treating compact digits as PII.

    Compact numbers are necessarily less distinctive than formatted phone
    numbers, so keep the regex permissive and reject invalid country codes,
    unusable area/exchange prefixes, N11 service-code blocks, and obvious
    generated-number false positives here.
    """
    digits = re.sub(r"[^0-9]", "", match.group("secret"))
    if len(digits) == 11:
        if digits[0] != "1":
            return False
        digits = digits[1:]
    if len(digits) != 10:
        return False

    area, exchange, subscriber = digits[:3], digits[3:6], digits[6:]
    if area[0] in "01" or exchange[0] in "01":
        return False
    if area[1:] == "11" or exchange[1:] == "11":
        return False
    if subscriber == "0000" or len(set(digits)) <= 2:
        return False

    # Reject wraparound counters such as 2345678901 and 9876543210. These
    # commonly appear as sample IDs and otherwise satisfy the NANP shape.
    deltas = [(int(right) - int(left)) % 10 for left, right in pairwise(digits)]
    return not (
        all(delta == 1 for delta in deltas) or all(delta == 9 for delta in deltas)
    )


def _valid_high_entropy(match: re.Match[str]) -> bool:
    token = match.group("secret")
    lowered = token.lower()
    if lowered.startswith(
        (
            "sk-",
            "sk_",
            "xox",
            "aiza",
            "npm_",
            "pypi-",
            "github_pat_",
            "ghp_",
            "gho_",
            "ghu_",
            "ghs_",
            "ghr_",
            "eyj",
        )
    ):
        return False
    if re.fullmatch(r"[0-9A-Fa-f]+", token) or len(set(token)) < 10:
        return False
    classes = sum(
        bool(regex.search(token))
        for regex in (
            re.compile(r"[a-z]"),
            re.compile(r"[A-Z]"),
            re.compile(r"[0-9]"),
            re.compile(r"[_+/=-]"),
        )
    )
    if classes < 3:
        return False
    entropy = -sum(
        (count / len(token)) * math.log2(count / len(token))
        for count in {
            character: token.count(character) for character in set(token)
        }.values()
    )
    return entropy >= 4.0


_PRIVATE_KEY_BODY_MAX_CHARS = 64 * 1024
_PATTERN_WHITESPACE_MAX_CHARS = 256
_EMAIL_LOCAL_MAX_CHARS = 64
_EMAIL_DOMAIN_MAX_CHARS = 253
_EMAIL_TLD_MAX_CHARS = 63
_PRIVATE_KEY_BLOCK_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Private key material",
    regex=re.compile(
        rf"(?P<secret>-----BEGIN (?P<pem_label>[A-Z0-9 ]{{0,64}}PRIVATE KEY)-----"
        rf"[\s\S]{{0,{_PRIVATE_KEY_BODY_MAX_CHARS}}}?"
        rf"-----END (?P=pem_label)-----)"
    ),
    secret_group="secret",
)
_PRIVATE_KEY_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Private key material",
    regex=re.compile(r"-----BEGIN [A-Z0-9 ]{0,64}PRIVATE KEY-----"),
)
_TOKEN_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Token or credential",
    regex=re.compile(
        r"(?<![A-Za-z0-9_])(?P<secret>"
        r"gh[pousr]_[A-Za-z0-9_]{20,255}|sk-(?:proj-)?[A-Za-z0-9]{20,255}"
        r"|sk-ant-[A-Za-z0-9_-]{20,255}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"
        r")(?![A-Za-z0-9_])"
    ),
    secret_group="secret",
)
_GITHUB_FINE_GRAINED_RULE = PatternRule(
    category="secret",
    severity="high",
    title="GitHub fine-grained token",
    regex=re.compile(
        r"(?<![A-Za-z0-9_])(?P<secret>github_pat_[A-Za-z0-9_]{22,255})(?![A-Za-z0-9_])"
    ),
    secret_group="secret",
)
_SLACK_TOKEN_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Slack token",
    regex=re.compile(
        r"(?<![A-Za-z0-9_-])(?P<secret>xox[a-z]-[A-Za-z0-9-]{10,255})(?![A-Za-z0-9-])",
        re.IGNORECASE,
    ),
    secret_group="secret",
)
_JWT_RULE = PatternRule(
    category="secret",
    severity="high",
    title="JSON Web Token",
    regex=re.compile(
        r"(?<![A-Za-z0-9_-])(?P<secret>[A-Za-z0-9_-]{8,512}\.[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{16,1024})(?![A-Za-z0-9_-])"
    ),
    secret_group="secret",
    validator=_valid_jwt,
)
_GOOGLE_API_KEY_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Google API key",
    regex=re.compile(
        r"(?<![A-Za-z0-9_-])(?P<secret>AIza[0-9A-Za-z_-]{35})(?![0-9A-Za-z_-])"
    ),
    secret_group="secret",
)
_STRIPE_LIVE_KEY_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Stripe live key",
    regex=re.compile(
        r"(?<![A-Za-z0-9_])(?P<secret>(?:sk|rk)_live_[0-9A-Za-z]{16,255})(?![0-9A-Za-z])"
    ),
    secret_group="secret",
)
_CONNECTION_URI_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Credentialed connection URI",
    regex=re.compile(
        r"\b[A-Za-z][A-Za-z0-9+.-]{1,31}://[^\s:/@]{1,128}:"
        r"(?P<secret>[^\s/@]{1,256})@[^\s/?#]{1,255}"
    ),
    secret_group="secret",
)
_CREDENTIAL_ASSIGNMENT_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Secret-like assignment",
    regex=re.compile(
        r"(?i)\b(?:token|secret|password|passwd|api[_-]?key|access[_-]?key)\b"
        rf"[\"']?\s{{0,{_PATTERN_WHITESPACE_MAX_CHARS}}}"
        rf"(?::|=|\bis\b)\s{{0,{_PATTERN_WHITESPACE_MAX_CHARS}}}"
        r"[\"']?(?P<secret>[A-Za-z0-9._\-/+=]{12,512})"
    ),
    secret_group="secret",
)
_BEARER_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Bearer credential",
    regex=re.compile(
        rf"(?i)\bbearer\s{{1,{_PATTERN_WHITESPACE_MAX_CHARS}}}"
        r"(?P<secret>[A-Za-z0-9._\-/+=]{20,1024})"
    ),
    secret_group="secret",
)
_BASIC_AUTH_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Basic authorization credential",
    regex=re.compile(
        rf"(?i)\bauthorization\s{{0,{_PATTERN_WHITESPACE_MAX_CHARS}}}:"
        rf"\s{{0,{_PATTERN_WHITESPACE_MAX_CHARS}}}basic"
        rf"\s{{1,{_PATTERN_WHITESPACE_MAX_CHARS}}}"
        r"(?P<secret>[A-Za-z0-9+/]{8,1020}={0,2})"
    ),
    secret_group="secret",
    validator=_valid_basic_auth,
)
_NPM_TOKEN_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Package registry token",
    regex=re.compile(
        r"(?<![A-Za-z0-9_])(?P<secret>npm_[A-Za-z0-9]{30,128})(?![A-Za-z0-9])"
    ),
    secret_group="secret",
)
_PYPI_TOKEN_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Package registry token",
    regex=re.compile(
        r"(?<![A-Za-z0-9_-])(?P<secret>pypi-[A-Za-z0-9_-]{40,255})(?![A-Za-z0-9_-])"
    ),
    secret_group="secret",
)
_REGISTRY_ASSIGNMENT_RULE = PatternRule(
    category="secret",
    severity="high",
    title="Package registry token",
    regex=re.compile(
        r"(?i)\b(?:_authToken|npmAuthToken|registry[_-]?token|pypi[_-]?token|nuget[_-]?key)\b"
        rf"[\"']?\s{{0,{_PATTERN_WHITESPACE_MAX_CHARS}}}"
        rf"(?::|=)\s{{0,{_PATTERN_WHITESPACE_MAX_CHARS}}}"
        r"[\"']?(?P<secret>[A-Za-z0-9._\-/+=]{12,512})"
    ),
    secret_group="secret",
)
_EMAIL_RULE = PatternRule(
    category="pii",
    severity="medium",
    title="Email address",
    regex=re.compile(
        rf"(?P<secret>[\w.+-]{{1,{_EMAIL_LOCAL_MAX_CHARS}}}"
        rf"@[\w.-]{{1,{_EMAIL_DOMAIN_MAX_CHARS}}}"
        rf"\.[A-Za-z]{{2,{_EMAIL_TLD_MAX_CHARS}}})"
    ),
    secret_group="secret",
)
_SSN_RULE = PatternRule(
    category="pii",
    severity="medium",
    title="US Social Security number",
    regex=re.compile(r"(?<!\d)(?P<secret>\d{3}-\d{2}-\d{4})(?!\d)"),
    secret_group="secret",
    validator=_valid_ssn,
)
_CARD_RULE = PatternRule(
    category="pii",
    severity="medium",
    title="Payment card number",
    regex=re.compile(r"(?<!\d)(?P<secret>(?:\d[ -]?){12,18}\d)(?!\d)"),
    secret_group="secret",
    validator=_valid_luhn_card,
)
_PHONE_RULE = PatternRule(
    category="pii",
    severity="medium",
    title="Phone number",
    regex=re.compile(
        rf"(?i)(?<!\d)(?:\b(?:phone|telephone|mobile|cell|tel)\b"
        rf"\s{{0,{_PATTERN_WHITESPACE_MAX_CHARS}}}[:=]?"
        rf"\s{{0,{_PATTERN_WHITESPACE_MAX_CHARS}}})?"
        r"(?P<secret>(?:\+?1[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]?\d{4})(?!\d)"
    ),
    secret_group="secret",
    validator=_valid_us_phone,
)
_INTERNATIONAL_PHONE_RULE = PatternRule(
    category="pii",
    severity="medium",
    title="Phone number",
    regex=re.compile(
        r"(?<!\w)(?P<secret>\+[1-9]\d{0,2}[ .-](?:\(?\d{2,4}\)?[ .-]){1,4}\d{3,4})(?!\d)"
    ),
    secret_group="secret",
    validator=_valid_international_phone,
)
_LABELED_PHONE_RULE = PatternRule(
    category="pii",
    severity="medium",
    title="Phone number",
    regex=re.compile(
        rf"(?i)\b(?:phone|telephone|mobile|cell|tel)\b"
        rf"\s{{0,{_PATTERN_WHITESPACE_MAX_CHARS}}}[:=]"
        rf"\s{{0,{_PATTERN_WHITESPACE_MAX_CHARS}}}"
        r"(?P<secret>\+?[1-9][0-9() .-]{6,24}\d)"
    ),
    secret_group="secret",
    validator=_valid_international_phone,
)
_HOME_PATH_SEGMENT_MAX_CHARS = 255
_HOME_PATH_DESCENDANT_MAX = 32

_HOME_PATH_RULE = PatternRule(
    category="pii",
    severity="medium",
    title="Absolute home path",
    regex=re.compile(
        rf"(?<!\w)(?P<secret>(?:"
        rf"/Users/[^/\s\"'`]{{1,{_HOME_PATH_SEGMENT_MAX_CHARS}}}|"
        rf"/home/[^/\s\"'`]{{1,{_HOME_PATH_SEGMENT_MAX_CHARS}}}|"
        rf"~/[^/\s\"'`]{{1,{_HOME_PATH_SEGMENT_MAX_CHARS}}})"
        rf"(?:/[^\s\"'`]{{1,{_HOME_PATH_SEGMENT_MAX_CHARS}}})"
        rf"{{0,{_HOME_PATH_DESCENDANT_MAX}}})"
    ),
    secret_group="secret",
)
_WINDOWS_HOME_PATH_RULE = PatternRule(
    category="pii",
    severity="medium",
    title="Windows home path",
    regex=re.compile(
        rf"(?i)(?<!\w)(?P<secret>"
        rf"[A-Z]:\\{{1,2}}Users\\{{1,2}}"
        rf"[^\\\s\"'`]{{1,{_HOME_PATH_SEGMENT_MAX_CHARS}}}"
        rf"(?:\\{{1,2}}[^\\\s\"'`]{{1,{_HOME_PATH_SEGMENT_MAX_CHARS}}})"
        rf"{{0,{_HOME_PATH_DESCENDANT_MAX}}})"
    ),
    secret_group="secret",
)
_HIGH_ENTROPY_RULE = PatternRule(
    category="secret",
    severity="high",
    title="High-entropy token",
    regex=re.compile(
        r"(?<![A-Za-z0-9_])(?P<secret>[A-Za-z0-9][A-Za-z0-9_+=-]{30,510}[A-Za-z0-9=])(?![A-Za-z0-9_])"
    ),
    secret_group="secret",
    validator=_valid_high_entropy,
)

_PATTERN_RULES = (
    _PRIVATE_KEY_BLOCK_RULE,
    _PRIVATE_KEY_RULE,
    _TOKEN_RULE,
    _GITHUB_FINE_GRAINED_RULE,
    _SLACK_TOKEN_RULE,
    _JWT_RULE,
    _GOOGLE_API_KEY_RULE,
    _STRIPE_LIVE_KEY_RULE,
    _CONNECTION_URI_RULE,
    _CREDENTIAL_ASSIGNMENT_RULE,
    _BEARER_RULE,
    _BASIC_AUTH_RULE,
    _NPM_TOKEN_RULE,
    _PYPI_TOKEN_RULE,
    _REGISTRY_ASSIGNMENT_RULE,
    _EMAIL_RULE,
    _SSN_RULE,
    _CARD_RULE,
    _PHONE_RULE,
    _INTERNATIONAL_PHONE_RULE,
    _LABELED_PHONE_RULE,
    _HOME_PATH_RULE,
    _WINDOWS_HOME_PATH_RULE,
    _HIGH_ENTROPY_RULE,
)

_VISIBILITY_ORDER = {"private": 0, "unlisted": 1, "public": 2}

# Session fields rendered on public detail/listing/analysis surfaces or used
# as their human-readable labels. Storage paths and visibility/review state are
# deliberately excluded: moving an identical artifact between managed roots
# is not publication metadata drift.
PUBLICATION_METADATA_FIELDS = (
    "source",
    "username",
    "display_name",
    "bio",
    "avatar_url",
    "machine",
    "project",
    "repo_name",
    "git_branch",
    "git_commit",
    "first_timestamp",
    "last_timestamp",
    "session_goal",
    "session_summary",
    "session_outcome",
    "session_status",
    "objective_family",
    "objective_label",
    "session_origin",
    "first_user_message",
    "model",
)


def publication_metadata_sha256(row) -> str:
    """Canonical fingerprint of the session metadata covered by review."""
    available = set(row.keys())

    def field_value(field_name: str):
        aliases = {
            "display_name": ("display_name", "user_display_name", "username"),
            "bio": ("bio", "user_bio"),
            "avatar_url": ("avatar_url", "user_avatar_url"),
        }
        if field_name in aliases:
            for candidate in aliases[field_name]:
                if candidate in available and row[candidate] is not None:
                    return row[candidate]
            return None
        return row[field_name] if field_name in available else None

    canonical = [
        [field_name, field_value(field_name)]
        for field_name in PUBLICATION_METADATA_FIELDS
    ]
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _needs_visibility_tightening(
    current_visibility: str, recommendation: str | None
) -> bool:
    if recommendation not in _VISIBILITY_ORDER:
        return False
    current_rank = _VISIBILITY_ORDER.get(
        current_visibility, max(_VISIBILITY_ORDER.values())
    )
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
            COALESCE(u.display_name, u.username, s.username) AS display_name,
            u.bio AS bio,
            u.avatar_url AS avatar_url
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
            COALESCE(u.display_name, u.username, s.username) AS display_name,
            u.bio AS bio,
            u.avatar_url AS avatar_url
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


_SCAN_CHUNK_BYTES = 1024 * 1024
# ``getwidth`` gives MAXWIDTH for an unbounded repeat. Refuse such patterns:
# the stream scanner can be correct with bounded memory only when every full
# regex match fits in the retained overlap.
_PATTERN_MAX_MATCH_CHARS = max(
    re._parser.parse(rule.regex.pattern, rule.regex.flags).getwidth()[1]
    for rule in _PATTERN_RULES
)
if _PATTERN_MAX_MATCH_CHARS >= re._parser.MAXWIDTH:
    raise RuntimeError("publish scanner patterns must all have a finite maximum width")
_SCAN_OVERLAP_CHARS = _PATTERN_MAX_MATCH_CHARS
# All configured left-boundary assertions inspect at most one prior character.
# Retain it so a match beginning exactly at the next committed offset cannot
# become a false positive merely because it appears at the start of a window.
_SCAN_PREFIX_CONTEXT_CHARS = 1
_EVIDENCE_CONTEXT_CHARS = 80
_MAX_EVIDENCE_PER_GROUP = 5


class _FindingAccumulator:
    """Retain bounded evidence while counting every detected occurrence."""

    def __init__(self) -> None:
        self.findings: list[PublishFinding] = []
        self._totals: dict[tuple[str, str, str, str], int] = {}
        self._kept: dict[tuple[str, str, str, str], int] = {}

    @staticmethod
    def _group(finding: PublishFinding) -> tuple[str, str, str, str]:
        return (
            finding.source,
            finding.category,
            finding.severity,
            finding.title,
        )

    def add(self, finding: PublishFinding) -> None:
        group = self._group(finding)
        occurrence = self._totals.get(group, 0) + 1
        self._totals[group] = occurrence
        finding.match_index = occurrence
        kept = self._kept.get(group, 0)
        if kept < _MAX_EVIDENCE_PER_GROUP:
            self.findings.append(finding)
            self._kept[group] = kept + 1

    def finalize(self) -> list[PublishFinding]:
        for finding in self.findings:
            group = self._group(finding)
            finding.match_count = self._totals[group]
            finding.omitted_count = self._totals[group] - self._kept[group]
        return self.findings


def _masked_excerpt(
    text: str,
    match_start: int,
    match_end: int,
    sensitive_ranges: list[tuple[int, int]],
) -> str:
    """Return match-centered context with the sensitive value already masked."""
    line_start = text.rfind("\n", 0, match_start) + 1
    next_newline = text.find("\n", match_end)
    line_end = len(text) if next_newline < 0 else next_newline
    excerpt_start = max(line_start, match_start - _EVIDENCE_CONTEXT_CHARS)
    excerpt_end = min(line_end, match_end + _EVIDENCE_CONTEXT_CHARS)
    clipped_ranges = sorted(
        (
            max(excerpt_start, start),
            min(excerpt_end, end),
        )
        for start, end in sensitive_ranges
        if start < excerpt_end and end > excerpt_start
    )
    merged_ranges: list[tuple[int, int]] = []
    for start, end in clipped_ranges:
        if merged_ranges and start <= merged_ranges[-1][1]:
            merged_ranges[-1] = (merged_ranges[-1][0], max(end, merged_ranges[-1][1]))
        else:
            merged_ranges.append((start, end))
    pieces: list[str] = []
    cursor = excerpt_start
    for start, end in merged_ranges:
        pieces.append(text[cursor:start])
        pieces.append("[MASKED]")
        cursor = end
    pieces.append(text[cursor:excerpt_end])
    masked = "".join(pieces)
    if excerpt_start > line_start:
        masked = f"…{masked}"
    if excerpt_end < line_end:
        masked = f"{masked}…"
    return _clip(masked)


def _matching_candidates(
    text: str,
) -> list[tuple[PatternRule, re.Match[str], int, int]]:
    candidates: list[tuple[PatternRule, re.Match[str], int, int]] = []
    for rule in _PATTERN_RULES:
        for match in rule.regex.finditer(text):
            if rule.validator is not None and not rule.validator(match):
                continue
            secret_start, secret_end = match.span(rule.secret_group)
            if secret_start >= 0 and secret_end > secret_start:
                candidates.append((rule, match, secret_start, secret_end))
    return candidates


def _mask_detected_values(value: str | None) -> str | None:
    """Mask every configured match before metadata/path output is exposed."""
    if not value:
        return value
    ranges = sorted((start, end) for _, _, start, end in _matching_candidates(value))
    if not ranges:
        return value
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.extend((value[cursor:start], "[MASKED]"))
        cursor = end
    pieces.append(value[cursor:])
    return "".join(pieces)


def _scan_text(
    text: str,
    source: str,
    findings: _FindingAccumulator,
    *,
    base_offset: int = 0,
    first_line_number: int = 1,
    emit_start_at_or_after: int | None = None,
    emit_start_before: int | None = None,
) -> None:
    """Scan text with ``finditer`` and serialize only masked evidence.

    Offsets are zero-based character offsets in ``source``.  The emission
    bounds let the streaming scanner rescan a fixed overlap without emitting
    duplicate boundary matches. They apply to the *full regex match start*,
    because any match starting before ``window_end - maximum_match_width`` is
    complete even when its end crosses that cutoff.
    """
    candidates = _matching_candidates(text)

    sensitive_ranges = [
        (secret_start, secret_end) for _, _, secret_start, secret_end in candidates
    ]
    emitted_in_window: set[tuple[str, str, str, int, int]] = set()
    for rule, match, secret_start, secret_end in candidates:
        full_start = base_offset + match.start()
        if emit_start_at_or_after is not None and full_start < emit_start_at_or_after:
            continue
        if emit_start_before is not None and full_start >= emit_start_before:
            continue
        finding_key = (
            rule.category,
            rule.severity,
            rule.title,
            base_offset + secret_start,
            base_offset + secret_end,
        )
        if finding_key in emitted_in_window:
            continue
        emitted_in_window.add(finding_key)
        line_number = first_line_number + text.count("\n", 0, secret_start)
        findings.add(
            PublishFinding(
                category=rule.category,
                severity=rule.severity,
                title=rule.title,
                evidence=_masked_excerpt(
                    text,
                    secret_start,
                    secret_end,
                    sensitive_ranges,
                ),
                source=source,
                line_number=line_number,
                match_start=base_offset + secret_start,
                match_end=base_offset + secret_end,
            )
        )


class _StreamingTextScanner:
    """Regex scanner that retains only a fixed overlap between text chunks."""

    def __init__(self, source: str, findings: _FindingAccumulator) -> None:
        self.source = source
        self.findings = findings
        self.tail = ""
        self.tail_line_number = 1
        self.total_chars = 0
        self.committed_start_offset = 0

    def feed(self, text: str, *, final: bool = False) -> None:
        window = self.tail + text
        window_base = self.total_chars - len(self.tail)
        window_end = window_base + len(window)
        cutoff = (
            window_end
            if final
            else max(
                window_base,
                window_end - _SCAN_OVERLAP_CHARS,
            )
        )
        _scan_text(
            window,
            self.source,
            self.findings,
            base_offset=window_base,
            first_line_number=self.tail_line_number,
            emit_start_at_or_after=self.committed_start_offset,
            emit_start_before=cutoff,
        )
        self.committed_start_offset = cutoff
        self.total_chars += len(text)

        if final:
            self.tail = ""
            return
        tail_length = min(
            _SCAN_OVERLAP_CHARS + _SCAN_PREFIX_CONTEXT_CHARS,
            len(window),
        )
        tail_start = len(window) - tail_length
        self.tail_line_number += window.count("\n", 0, tail_start)
        self.tail = window[tail_start:]


def _scan_file(
    path: Path,
    findings: _FindingAccumulator,
    *,
    stage_dir: Path | None,
) -> tuple[str, int, str | None]:
    """Hash, scan, and optionally stage a file in one bounded binary pass."""
    staged_path: Path | None = None
    stage_file = None
    if stage_dir is not None:
        stage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, raw_staged_path = tempfile.mkstemp(
            prefix=".publish-review-",
            suffix=".stage",
            dir=stage_dir,
        )
        os.fchmod(fd, 0o600)
        staged_path = Path(raw_staged_path)
        stage_file = os.fdopen(fd, "wb")

    digest = hashlib.sha256()
    inspected_size = 0
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    scanner = _StreamingTextScanner(
        _mask_detected_values(str(path)) or "session file",
        findings,
    )
    try:
        with path.open("rb") as source_file:
            while True:
                chunk = source_file.read(_SCAN_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                inspected_size += len(chunk)
                if stage_file is not None:
                    stage_file.write(chunk)
                scanner.feed(decoder.decode(chunk))
        scanner.feed(decoder.decode(b"", final=True), final=True)
        if stage_file is not None:
            stage_file.flush()
            os.fsync(stage_file.fileno())
            stage_file.close()
            stage_file = None
        return (
            digest.hexdigest(),
            inspected_size,
            str(staged_path) if staged_path else None,
        )
    except Exception:
        if stage_file is not None:
            stage_file.close()
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
        raise


def _scan_metadata(row, findings: _FindingAccumulator) -> None:
    metadata_fields = (
        "display_name",
        "bio",
        "avatar_url",
        "username",
        "repo_name",
        "project",
        "first_user_message",
        "machine",
        "model",
        "git_branch",
        "git_commit",
        "session_goal",
        "session_summary",
        "session_outcome",
        "session_status",
        "session_origin",
        "objective_family",
        "objective_label",
        "source_path",
        "shared_path",
        "workspace_root",
        "worktree_root",
        "repo_root",
    )
    available_fields = set(row.keys())
    for field_name in metadata_fields:
        if field_name not in available_fields:
            continue
        value = row[field_name]
        if not value:
            continue
        text = f"{field_name}: {value}"
        _scan_text(text, f"metadata.{field_name}", findings)


def _dedupe_findings(findings: list[PublishFinding]) -> list[PublishFinding]:
    seen: set[tuple[str, str, str, str, int | None, int | None, int | None]] = set()
    unique: list[PublishFinding] = []
    for finding in findings:
        key = (
            finding.category,
            finding.severity,
            finding.title,
            finding.source,
            finding.line_number,
            finding.match_start,
            finding.match_end,
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
    *,
    stage_dir: Path | None = None,
) -> PublishReview | None:
    row = _load_session_row(conn, session_id_prefix)
    if not row:
        return None

    inspected_path = _inspect_path(row)
    inspected_sha256: str | None = None
    inspected_size = 0
    staged_path: str | None = None
    finding_accumulator = _FindingAccumulator()
    if inspected_path and inspected_path.exists():
        try:
            inspected_sha256, inspected_size, staged_path = _scan_file(
                inspected_path,
                finding_accumulator,
                stage_dir=stage_dir,
            )
        except OSError:
            finding_accumulator.add(
                PublishFinding(
                    category="missing",
                    severity="high",
                    title="Session file unreadable",
                    evidence=f"Could not read the session file for {row['session_id']}",
                    source="metadata",
                    line_number=None,
                )
            )
    else:
        finding_accumulator.add(
            PublishFinding(
                category="missing",
                severity="high",
                title="Session file missing",
                evidence=f"Could not find shared/source session file for {row['session_id']}",
                source="metadata",
                line_number=None,
            )
        )

    _scan_metadata(row, finding_accumulator)
    findings = _dedupe_findings(finding_accumulator.finalize())
    findings = sorted(
        findings,
        key=lambda item: (
            0 if item.severity == "high" else 1 if item.severity == "medium" else 2,
            item.source,
            item.line_number or 0,
            item.match_start if item.match_start is not None else -1,
            item.title,
        ),
    )
    recommendation, rationale = _recommendation(findings)
    metadata = {
        "display_name": row["display_name"],
        "bio": row["bio"],
        "avatar_url": row["avatar_url"],
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
        "session_goal": row["session_goal"],
        "session_summary": row["session_summary"],
        "session_outcome": row["session_outcome"],
        "objective_label": row["objective_label"],
    }
    safe_metadata = {
        key: _mask_detected_values(value) if isinstance(value, str) else value
        for key, value in metadata.items()
    }
    return PublishReview(
        session_id=row["session_id"],
        source=row["source"],
        current_visibility=row["visibility"],
        visibility_source=row["visibility_source"],
        recommendation=recommendation,
        rationale=rationale,
        inspected_path=(
            _mask_detected_values(str(inspected_path)) if inspected_path else None
        ),
        inspected_sha256=inspected_sha256,
        inspected_size=inspected_size,
        staged_path=staged_path,
        metadata_sha256=publication_metadata_sha256(row),
        metadata=safe_metadata,
        findings=findings,
    )


def _absolute_path(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _secure_managed_directory(path: Path, root: Path) -> None:
    """Create/validate a 0700 directory chain without following symlinks."""
    path = _absolute_path(path)
    root = _absolute_path(root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Managed publish path escapes its root: {path}") from exc
    if not root.exists():
        root.mkdir(mode=0o700, parents=True)
    current = root
    for component in (Path(), *relative.parts):
        if component != Path():
            current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            mode = current.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError(f"Refusing unsafe publish directory: {current}")
        current.chmod(0o700)


def review_staging_dir(shared_dir: Path) -> Path:
    """Return the validated private staging directory used during approval."""
    shared_root = _absolute_path(shared_dir)
    staging_root = shared_root / ".review-staging"
    _secure_managed_directory(staging_root, shared_root)
    return staging_root


def _digest_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, _SCAN_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def preserve_reviewed_artifact(
    conn: sqlite3.Connection,
    review: PublishReview,
    *,
    shared_dir: Path,
    approved_visibility: str,
    forced: bool = False,
) -> int:
    """Persist the exact staged review bytes and return its review record id."""
    if not review.staged_path or not review.inspected_sha256:
        raise ValueError("Approval requires a staged, hashed publish review")
    if not re.fullmatch(r"[0-9a-f]{64}", review.inspected_sha256):
        raise ValueError("Publish review has an invalid SHA-256")
    if approved_visibility not in _VISIBILITY_ORDER:
        raise ValueError(f"Unsupported approved visibility: {approved_visibility}")
    allowed, refusal = can_apply_visibility(
        review,
        approved_visibility,
        force=forced,
    )
    if not allowed:
        raise ValueError(refusal)

    row = conn.execute(
        """
        SELECT s.*, COALESCE(u.display_name, u.username, s.username) AS display_name,
               u.bio AS bio, u.avatar_url AS avatar_url
        FROM sessions s
        LEFT JOIN users u ON u.username = s.username
        WHERE s.session_id = ?
        """,
        (review.session_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Session disappeared during review: {review.session_id}")
    indexed_hash = row["file_hash"] or ""
    if not indexed_hash or not review.inspected_sha256.startswith(indexed_hash):
        raise ValueError(
            "Session source changed since its indexed revision; sync and review it again."
        )
    current_metadata_sha256 = publication_metadata_sha256(row)
    if not review.metadata_sha256 or review.metadata_sha256 != current_metadata_sha256:
        raise ValueError(
            "Session metadata changed during review; review the current metadata again."
        )

    shared_root = _absolute_path(shared_dir)
    staging_root = review_staging_dir(shared_root)
    publish_root = shared_root / ".published"
    _secure_managed_directory(publish_root, shared_root)

    staged_path = _absolute_path(review.staged_path)
    try:
        staged_relative = staged_path.relative_to(staging_root)
    except ValueError as exc:
        raise ValueError(
            f"Refusing review staging file outside managed staging root: {staged_path}"
        ) from exc
    current = staging_root
    for component in staged_relative.parts[:-1]:
        current /= component
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError(f"Refusing unsafe review staging parent: {current}")

    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    source_flags |= getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(staged_path, source_flags)
    artifact_path: Path
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError(
                f"Review staging artifact is not a regular file: {staged_path}"
            )
        if source_stat.st_size != review.inspected_size:
            raise ValueError("Review staging size changed before approval")
        if _digest_fd(source_fd) != review.inspected_sha256:
            raise ValueError("Review staging hash changed before approval")

        safe_session_id = (
            re.sub(r"[^A-Za-z0-9._-]+", "-", review.session_id).strip(".-") or "session"
        )
        artifact_parent = publish_root / safe_session_id
        _secure_managed_directory(artifact_parent, publish_root)
        artifact_path = artifact_parent / f"{review.inspected_sha256}.jsonl"

        parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        parent_flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(artifact_parent, parent_flags)
        temp_name = f".{artifact_path.name}.{secrets.token_hex(8)}.tmp-review"
        temp_fd = -1
        try:
            try:
                target_stat = os.stat(
                    artifact_path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                target_stat = None
            if target_stat is not None:
                if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(
                    target_stat.st_mode
                ):
                    raise ValueError(
                        f"Reviewed artifact is not a regular file: {artifact_path}"
                    )
                target_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                target_flags |= getattr(os, "O_NOFOLLOW", 0)
                target_fd = os.open(artifact_path.name, target_flags, dir_fd=parent_fd)
                try:
                    if _digest_fd(target_fd) != review.inspected_sha256:
                        raise ValueError(
                            f"Hash-addressed reviewed artifact is corrupt: {artifact_path}"
                        )
                finally:
                    os.close(target_fd)
            else:
                if shutil.disk_usage(artifact_parent).free < review.inspected_size:
                    raise OSError(
                        errno.ENOSPC, "not enough free space for reviewed artifact"
                    )
                temp_fd = os.open(
                    temp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_fd,
                )
                os.lseek(source_fd, 0, os.SEEK_SET)
                with os.fdopen(temp_fd, "wb") as temp_file:
                    temp_fd = -1
                    while True:
                        chunk = os.read(source_fd, _SCAN_CHUNK_BYTES)
                        if not chunk:
                            break
                        temp_file.write(chunk)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                os.replace(
                    temp_name,
                    artifact_path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                os.chmod(artifact_path, 0o600, follow_symlinks=False)
                verify_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                verify_flags |= getattr(os, "O_NOFOLLOW", 0)
                verify_fd = os.open(artifact_path.name, verify_flags, dir_fd=parent_fd)
                try:
                    if _digest_fd(verify_fd) != review.inspected_sha256:
                        raise ValueError(
                            "Reviewed artifact failed post-copy verification"
                        )
                finally:
                    os.close(verify_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
    finally:
        os.close(source_fd)

    reviewed_at = datetime.now(UTC).isoformat()
    cur = conn.execute(
        """
        INSERT INTO publication_reviews (
            session_id, reviewed_sha256, reviewed_artifact_path,
            reviewed_metadata_sha256,
            recommendation, approved_visibility, forced, successful, reviewed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            review.session_id,
            review.inspected_sha256,
            str(artifact_path),
            current_metadata_sha256,
            review.recommendation,
            approved_visibility,
            1 if forced else 0,
            reviewed_at,
        ),
    )
    review_id = int(cur.lastrowid)
    conn.execute(
        """
        UPDATE sessions
        SET reviewed_sha256 = ?,
            reviewed_artifact_path = ?,
            publication_metadata_sha256 = ?,
            reviewed_metadata_sha256 = ?,
            publication_review_id = ?,
            publication_state = 'reviewed'
        WHERE session_id = ?
        """,
        (
            review.inspected_sha256,
            str(artifact_path),
            current_metadata_sha256,
            current_metadata_sha256,
            review_id,
            review.session_id,
        ),
    )
    return review_id


def _validated_public_artifact_path(
    row,
    *,
    shared_dir: Path,
) -> tuple[Path, str] | None:
    if row["visibility"] != "public":
        return None
    reviewed_metadata = row["reviewed_metadata_sha256"]
    current_metadata = publication_metadata_sha256(row)
    if (
        not reviewed_metadata
        or row["publication_metadata_sha256"] != reviewed_metadata
        or current_metadata != reviewed_metadata
    ):
        return None
    expected = row["reviewed_sha256"]
    raw_path = row["reviewed_artifact_path"]
    if not expected or not raw_path or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return None
    shared_root = _absolute_path(shared_dir)
    publish_root = shared_root / ".published"
    artifact = _absolute_path(raw_path)
    try:
        shared_mode = shared_root.lstat().st_mode
        if stat.S_ISLNK(shared_mode) or not stat.S_ISDIR(shared_mode):
            return None
        relative = artifact.relative_to(publish_root)
    except ValueError:
        return None
    current = publish_root
    try:
        root_mode = current.lstat().st_mode
        if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
            return None
        for index, component in enumerate(relative.parts):
            current /= component
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                return None
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(mode):
                return None
        if not relative.parts or not stat.S_ISREG(current.lstat().st_mode):
            return None
    except OSError:
        return None
    return artifact, expected


@contextmanager
def open_verified_public_artifact(
    row,
    *,
    shared_dir: Path,
) -> Iterator[TextIO | None]:
    """Yield the same O_NOFOLLOW file description that passed hash review."""
    validated = _validated_public_artifact_path(row, shared_dir=shared_dir)
    if validated is None:
        yield None
        return
    artifact, expected = validated
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(artifact, flags)
    except OSError:
        yield None
        return
    stream = None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or _digest_fd(fd) != expected:
            os.close(fd)
            fd = -1
            yield None
            return
        os.lseek(fd, 0, os.SEEK_SET)
        stream = os.fdopen(fd, "r", encoding="utf-8", errors="replace")
        fd = -1
        yield stream
    finally:
        if stream is not None:
            stream.close()
        if fd >= 0:
            os.close(fd)


def resolve_verified_public_artifact(row, *, shared_dir: Path) -> Path | None:
    """Verify an artifact path for diagnostics; servers use the open-fd API."""
    with open_verified_public_artifact(row, shared_dir=shared_dir) as stream:
        if stream is None:
            return None
        return _absolute_path(row["reviewed_artifact_path"])


def serialize_publish_review(review: PublishReview) -> dict:
    return {
        "session_id": review.session_id,
        "source": review.source,
        "current_visibility": review.current_visibility,
        "visibility_source": review.visibility_source,
        "recommendation": review.recommendation,
        "rationale": review.rationale,
        "inspected_path": review.inspected_path,
        "inspected_sha256": review.inspected_sha256,
        "inspected_size": review.inspected_size,
        "metadata_sha256": review.metadata_sha256,
        "metadata": dict(review.metadata),
        "findings": [
            {
                "category": finding.category,
                "severity": finding.severity,
                "title": finding.title,
                "evidence": finding.evidence,
                "source": finding.source,
                "line_number": finding.line_number,
                "match_start": finding.match_start,
                "match_end": finding.match_end,
                "match_index": finding.match_index,
                "match_count": finding.match_count,
                "omitted_count": finding.omitted_count,
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
    fetch_limit = (
        None if normalized_visibility == "needs_changes" else max(1, min(limit, 200))
    )

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
            username=_mask_detected_values(row["username"]) or "[MASKED]",
            display_name=_mask_detected_values(row["display_name"]) or "[MASKED]",
            session_origin=row["session_origin"],
            project=_mask_detected_values(row["project"]),
            repo_name=_mask_detected_values(row["repo_name"]),
            visibility=row["visibility"],
            first_timestamp=row["first_timestamp"],
            last_timestamp=row["last_timestamp"],
            session_status=row["session_status"],
            session_goal=_mask_detected_values(row["session_goal"]),
            session_summary=_mask_detected_values(row["session_summary"]),
            session_outcome=_mask_detected_values(row["session_outcome"]),
        )
        if include_reviews or normalized_visibility == "needs_changes":
            review = review_publish_session(conn, candidate.session_id)
            if review:
                candidate.review_recommendation = review.recommendation
                candidate.review_rationale = review.rationale
                candidate.finding_count = len(review.findings)
                candidate.high_findings = sum(
                    1 for finding in review.findings if finding.severity == "high"
                )
                candidate.medium_findings = sum(
                    1 for finding in review.findings if finding.severity == "medium"
                )
                if (
                    normalized_visibility == "needs_changes"
                    and not _needs_visibility_tightening(
                        candidate.visibility,
                        review.recommendation,
                    )
                ):
                    continue
        elif normalized_visibility == "needs_changes":
            continue
        candidates.append(candidate)
        if normalized_visibility == "needs_changes" and len(candidates) >= max(
            1, min(limit, 200)
        ):
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
            if review and _needs_visibility_tightening(
                row["visibility"], review.recommendation
            ):
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
        if finding.match_start is not None and finding.match_end is not None:
            location = f"{location} offsets {finding.match_start}:{finding.match_end}"
        if finding.match_count > 1:
            location = f"{location} match {finding.match_index}/{finding.match_count}"
        if finding.match_index == 1 and finding.omitted_count:
            location = f"{location}; {finding.omitted_count} evidence item(s) omitted"
        lines.append(
            f"- [{finding.severity}] {finding.category}: {finding.title} ({location})"
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


def can_apply_visibility(
    review: PublishReview, target_visibility: str, force: bool = False
) -> tuple[bool, str]:
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
