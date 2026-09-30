"""Parse Claude Code and Codex JSONL session files."""

import base64
import binascii
import copy
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    tool_name: str
    command: str | None = None
    timestamp: str | None = None
    is_error: bool = False
    operation: str = "other"
    input_paths: list[str] = field(default_factory=list)
    command_paths: list[str] = field(default_factory=list)
    call_id: str | None = None


@dataclass
class DailyUsage:
    """Per-UTC-day slice of a session's usage, bucketed by event timestamps.

    Sessions can span weeks; attributing whole-session totals to the start
    date distorts any date-bucketed rollup. Usage without a usable timestamp
    is reconciled onto a deterministic residual day and marked approximated,
    so every daily component sums exactly to the session row.
    """

    day: str  # YYYY-MM-DD (UTC)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    fresh_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    cache_creation_unknown_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    user_message_count: int = 0
    assistant_message_count: int = 0
    tool_call_count: int = 0
    approximated: bool = False


@dataclass
class MessageUsage:
    """One deduplicated assistant message's usage, for cross-session claims.

    claim_key matches the usage-tracker pipeline: "mid:rid" when both are
    present (replayed copies preserve message.id, requestId, uuid, and
    timestamps verbatim — only sessionId is re-stamped), falling back to
    "uuid:<uuid>" then "mid:<message.id>". Records logpile already drops
    (no message.id) emit no claim.
    """

    claim_key: str
    day: str | None  # YYYY-MM-DD (UTC), None when the record has no timestamp
    model: str | None
    fresh_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    cache_creation_unknown_input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class SessionPath:
    raw_path: str
    normalized_path: str
    display_path: str
    relative_path: str | None
    operation: str
    source: str
    repo_relative_path: str | None = None
    tool_name: str | None = None
    timestamp: str | None = None


@dataclass
class SessionInfo:
    session_id: str
    source: str  # 'claudecode' or 'codex'
    project: str
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    user_message_count: int = 0
    assistant_message_count: int = 0
    tool_call_count: int = 0
    error_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    fresh_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    cache_creation_unknown_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    first_user_message: str = ""
    model: str | None = None
    workspace_root: str | None = None
    thread_id: str | None = None
    parent_thread_id: str | None = None
    parent_session_id: str | None = None
    spawn_depth: int = 0
    tool_calls: Sequence[ToolCall] = field(default_factory=list)
    session_paths: Sequence[SessionPath] = field(default_factory=list)
    daily_usage: Sequence[DailyUsage] = field(default_factory=list)
    message_usage: Sequence[MessageUsage] = field(
        default_factory=list
    )  # claudecode only


class _SqliteSequence(Sequence):
    """Small reusable sequence view over a parser's disk-backed spool.

    A transcript can contain millions of assistant messages and tool calls.
    Keeping those output rows in Python lists merely moves the whole-file
    memory problem one layer down from JSON decoding.  These views retain the
    familiar ``len``/index/iteration behavior without retaining every row in
    the Python heap.
    """

    def __init__(self, spool, table: str, columns: str, factory) -> None:
        self._spool = spool
        self._table = table
        self._columns = columns
        self._factory = factory

    def __len__(self) -> int:
        row = self._spool.connection.execute(
            f"SELECT COUNT(*) FROM {self._table}"
        ).fetchone()
        return int(row[0])

    def __iter__(self):
        rows = self._spool.connection.execute(
            f"SELECT {self._columns} FROM {self._table} ORDER BY seq"
        )
        for row in rows:
            yield self._factory(row)

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            return [self[position] for position in range(start, stop, step)]
        if index < 0:
            index += len(self)
        if index < 0:
            raise IndexError(index)
        row = self._spool.connection.execute(
            f"SELECT {self._columns} FROM {self._table} ORDER BY seq LIMIT 1 OFFSET ?",
            (index,),
        ).fetchone()
        if row is None:
            raise IndexError(index)
        return self._factory(row)


_SPOOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    fresh_input INTEGER NOT NULL,
    cached_input INTEGER NOT NULL,
    cache_creation INTEGER NOT NULL,
    cache_creation_5m INTEGER NOT NULL,
    cache_creation_1h INTEGER NOT NULL,
    cache_creation_unknown INTEGER NOT NULL,
    output INTEGER NOT NULL,
    model TEXT,
    timestamp TEXT,
    request_id TEXT,
    uuid TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    command TEXT,
    timestamp TEXT,
    is_error INTEGER NOT NULL DEFAULT 0,
    operation TEXT NOT NULL,
    input_paths_json TEXT NOT NULL,
    command_paths_json TEXT NOT NULL,
    call_id_json TEXT
);
CREATE INDEX IF NOT EXISTS tool_calls_call_id
    ON tool_calls(call_id_json) WHERE call_id_json IS NOT NULL;
CREATE TABLE IF NOT EXISTS session_paths (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_path TEXT NOT NULL,
    normalized_path TEXT NOT NULL,
    display_path TEXT NOT NULL,
    relative_path TEXT,
    operation TEXT NOT NULL,
    source TEXT NOT NULL,
    tool_name TEXT,
    timestamp TEXT
);
CREATE TABLE IF NOT EXISTS spool_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class _ParseSpool:
    """Mode-0600 SQLite spill storage for cardinality-dependent parse state.

    Without ``path`` the spool is a throwaway file in a private temporary
    directory. With ``path`` it is a resumable parse state that incremental
    sync keeps between runs: the caller owns the enclosing 0700 directory,
    and transactions are explicit (``begin``/``commit``; ``close`` rolls back
    anything uncommitted) so a provisional tail never becomes durable state.
    """

    def __init__(self, path: Path | None = None, *, in_memory: bool = False) -> None:
        self._temporary_directory = None
        self.persistent = path is not None
        if in_memory and path is None:
            # Small transcripts: the spill file only bounds memory for huge
            # ones, and creating it costs more than the parse itself.
            self.connection = sqlite3.connect(":memory:")
        elif path is None:
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix="logpile-session-parse-"
            )
            path = Path(self._temporary_directory.name) / "state.sqlite"
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            self.connection = sqlite3.connect(path)
            # Bound SQLite's own page cache and keep sensitive transcript-derived
            # state on disk in the mode-0700 temporary directory.
            self.connection.execute("PRAGMA journal_mode = OFF")
            self.connection.execute("PRAGMA synchronous = OFF")
        else:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            self.connection = sqlite3.connect(path, isolation_level=None)
            # A rollback journal lets close() discard a provisional tail; the
            # generation check in sync turns any torn state into a full parse.
            self.connection.execute("PRAGMA journal_mode = DELETE")
            self.connection.execute("PRAGMA synchronous = NORMAL")
        self.connection.execute("PRAGMA temp_store = FILE")
        self.connection.execute("PRAGMA cache_size = -1024")
        self.connection.execute("PRAGMA mmap_size = 0")
        self.connection.executescript(_SPOOL_SCHEMA)

    def begin(self) -> None:
        if self.persistent and not self.connection.in_transaction:
            self.connection.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        if self.persistent and self.connection.in_transaction:
            self.connection.execute("COMMIT")

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM spool_meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO spool_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def close(self) -> None:
        connection = getattr(self, "connection", None)
        if connection is not None:
            self.connection = None
            try:
                if getattr(self, "persistent", False) and connection.in_transaction:
                    connection.execute("ROLLBACK")
            finally:
                connection.close()
        temporary_directory = getattr(self, "_temporary_directory", None)
        if temporary_directory is not None:
            self._temporary_directory = None
            temporary_directory.cleanup()

    def __del__(self) -> None:
        self.close()

    @staticmethod
    def _json_scalar(value: Any) -> str | None:
        if value is None:
            return None
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return json.dumps(str(value), ensure_ascii=False)

    def put_message(
        self,
        message_id: str,
        *,
        fresh_input: int,
        cached_input: int,
        cache_creation: int,
        cache_creation_5m: int,
        cache_creation_1h: int,
        cache_creation_unknown: int,
        output: int,
        model: str | None,
        timestamp: str | None,
        request_id: Any,
        uuid: Any,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO messages (
                message_id, fresh_input, cached_input, cache_creation,
                cache_creation_5m, cache_creation_1h,
                cache_creation_unknown, output, model, timestamp,
                request_id, uuid
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                fresh_input = excluded.fresh_input,
                cached_input = excluded.cached_input,
                cache_creation = excluded.cache_creation,
                cache_creation_5m = excluded.cache_creation_5m,
                cache_creation_1h = excluded.cache_creation_1h,
                cache_creation_unknown = excluded.cache_creation_unknown,
                output = excluded.output,
                model = excluded.model,
                timestamp = excluded.timestamp,
                request_id = excluded.request_id,
                uuid = excluded.uuid
            WHERE excluded.output > messages.output
            """,
            (
                message_id,
                fresh_input,
                cached_input,
                cache_creation,
                cache_creation_5m,
                cache_creation_1h,
                cache_creation_unknown,
                output,
                model,
                timestamp,
                _string_id(request_id),
                _string_id(uuid),
            ),
        )

    def append_tool_call(self, tool_call: ToolCall) -> None:
        self.connection.execute(
            """
            INSERT INTO tool_calls (
                tool_name, command, timestamp, is_error, operation,
                input_paths_json, command_paths_json, call_id_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tool_call.tool_name,
                tool_call.command,
                tool_call.timestamp,
                1 if tool_call.is_error else 0,
                tool_call.operation,
                json.dumps(tool_call.input_paths, ensure_ascii=False),
                json.dumps(tool_call.command_paths, ensure_ascii=False),
                self._json_scalar(tool_call.call_id),
            ),
        )

    def set_tool_result(self, call_id: Any, is_error: bool) -> None:
        call_id_json = self._json_scalar(call_id)
        if call_id_json is None:
            return
        self.connection.execute(
            """
            UPDATE tool_calls
            SET is_error = ?
            WHERE seq = (
                SELECT MAX(seq) FROM tool_calls WHERE call_id_json = ?
            )
            """,
            (1 if is_error else 0, call_id_json),
        )

    @staticmethod
    def _tool_call(row) -> ToolCall:
        return ToolCall(
            tool_name=row[0],
            command=row[1],
            timestamp=row[2],
            is_error=bool(row[3]),
            operation=row[4],
            input_paths=json.loads(row[5]),
            command_paths=json.loads(row[6]),
            call_id=json.loads(row[7]) if row[7] is not None else None,
        )

    def tool_calls(self) -> Sequence[ToolCall]:
        return _SqliteSequence(
            self,
            "tool_calls",
            (
                "tool_name, command, timestamp, is_error, operation, "
                "input_paths_json, command_paths_json, call_id_json"
            ),
            self._tool_call,
        )

    def message_totals(self) -> tuple[int, ...]:
        row = self.connection.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(fresh_input), 0),
                   COALESCE(SUM(cached_input), 0),
                   COALESCE(SUM(cache_creation), 0),
                   COALESCE(SUM(cache_creation_5m), 0),
                   COALESCE(SUM(cache_creation_1h), 0),
                   COALESCE(SUM(cache_creation_unknown), 0),
                   COALESCE(SUM(output), 0)
            FROM messages
            """
        ).fetchone()
        return tuple(int(value) for value in row)

    def iter_message_state(self):
        yield from self.connection.execute(
            """
            SELECT message_id, fresh_input, cached_input, cache_creation,
                   cache_creation_5m, cache_creation_1h,
                   cache_creation_unknown, output, model, timestamp,
                   request_id, uuid
            FROM messages ORDER BY seq
            """
        )

    @staticmethod
    def _message_usage(row, fallback_day: str) -> MessageUsage:
        message_id = row[0]
        if row[10]:
            claim_key = f"{message_id}:{row[10]}"
        elif row[11]:
            claim_key = f"uuid:{row[11]}"
        else:
            claim_key = f"mid:{message_id}"
        return MessageUsage(
            claim_key=claim_key,
            day=_day_of(row[9]) or fallback_day,
            model=row[8],
            fresh_input_tokens=row[1],
            cached_input_tokens=row[2],
            cache_creation_input_tokens=row[3],
            cache_creation_5m_input_tokens=row[4],
            cache_creation_1h_input_tokens=row[5],
            cache_creation_unknown_input_tokens=row[6],
            output_tokens=row[7],
        )

    def message_usage(self, fallback_day: str) -> Sequence[MessageUsage]:
        return _SqliteSequence(
            self,
            "messages",
            (
                "message_id, fresh_input, cached_input, cache_creation, "
                "cache_creation_5m, cache_creation_1h, "
                "cache_creation_unknown, output, model, timestamp, "
                "request_id, uuid"
            ),
            lambda row: self._message_usage(row, fallback_day),
        )

    def ensure_session_paths(self, workspace_root: str | None) -> None:
        """Extend path rows to cover every spooled tool call.

        Paths depend only on each call and the workspace root, so a resumed
        parse extends them from the last covered call; a changed root
        renormalizes from scratch. Rows stay in tool-call order either way.
        """
        workspace_key = json.dumps(workspace_root)
        covered = self.get_meta("paths_workspace")
        through = int(self.get_meta("paths_through_seq") or 0)
        if covered != workspace_key:
            self.connection.execute("DELETE FROM session_paths")
            through = 0
        last_seq = through
        for seq, tool_call in self._tool_calls_after(through):
            last_seq = seq
            for source_name, candidates in (
                ("tool_input", tool_call.input_paths),
                ("command", tool_call.command_paths),
            ):
                for candidate in _unique_preserve_order(candidates):
                    normalized = _normalize_session_path(candidate, workspace_root)
                    if not normalized:
                        continue
                    normalized_path, relative_path, display_path = normalized
                    self.connection.execute(
                        """
                        INSERT INTO session_paths (
                            raw_path, normalized_path, display_path,
                            relative_path, operation, source, tool_name,
                            timestamp
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            candidate,
                            normalized_path,
                            display_path,
                            relative_path,
                            tool_call.operation,
                            source_name,
                            tool_call.tool_name,
                            tool_call.timestamp,
                        ),
                    )
        self.set_meta("paths_workspace", workspace_key)
        self.set_meta("paths_through_seq", str(last_seq))

    def _tool_calls_after(self, seq: int) -> Iterator[tuple[int, ToolCall]]:
        rows = self.connection.execute(
            """
            SELECT seq, tool_name, command, timestamp, is_error, operation,
                   input_paths_json, command_paths_json, call_id_json
            FROM tool_calls WHERE seq > ? ORDER BY seq
            """,
            (seq,),
        )
        for row in rows:
            yield row[0], self._tool_call(row[1:])

    @staticmethod
    def _session_path(row) -> SessionPath:
        return SessionPath(
            raw_path=row[0],
            normalized_path=row[1],
            display_path=row[2],
            relative_path=row[3],
            operation=row[4],
            source=row[5],
            tool_name=row[6],
            timestamp=row[7],
        )

    def session_paths(self) -> Sequence[SessionPath]:
        return _SqliteSequence(
            self,
            "session_paths",
            (
                "raw_path, normalized_path, display_path, relative_path, "
                "operation, source, tool_name, timestamp"
            ),
            self._session_path,
        )


@dataclass(frozen=True)
class PrivateSessionMarker:
    """A valid transcript that explicitly opts out of shared indexing."""

    session_id: str
    source: str
    marker: str


@dataclass
class JsonlLoadStats:
    """Structured counts for records skipped while loading one JSONL file."""

    invalid_json_lines: int = 0
    malformed_record_types: dict[str, int] = field(default_factory=dict)
    malformed_fields: dict[str, int] = field(default_factory=dict)
    record_exceptions: dict[str, int] = field(default_factory=dict)
    io_errors: int = 0

    @property
    def malformed_record_count(self) -> int:
        return (
            self.invalid_json_lines
            + sum(self.malformed_record_types.values())
            + sum(self.malformed_fields.values())
            + sum(self.record_exceptions.values())
        )

    def count_record_type(self, value: Any) -> None:
        record_type = _json_type_name(value)
        self.malformed_record_types[record_type] = (
            self.malformed_record_types.get(record_type, 0) + 1
        )

    def count_exception(self, exc: Exception) -> None:
        exception_type = type(exc).__name__
        self.record_exceptions[exception_type] = (
            self.record_exceptions.get(exception_type, 0) + 1
        )

    def count_malformed_field(self, field_name: str, value: Any) -> None:
        key = f"{field_name}:{_json_type_name(value)}"
        self.malformed_fields[key] = self.malformed_fields.get(key, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "invalid_json_lines": self.invalid_json_lines,
            "malformed_record_types": dict(self.malformed_record_types),
            "malformed_fields": dict(self.malformed_fields),
            "record_exceptions": dict(self.record_exceptions),
            "io_errors": self.io_errors,
            "malformed_record_count": self.malformed_record_count,
        }


_USER_CONTEXT_PREAMBLE_RE = re.compile(
    r"#\s*Context from my IDE setup:.*?## My request for Codex:\n",
    flags=re.DOTALL,
)
_HARNESS_OPEN_TAG_RE = re.compile(r"^<([A-Za-z][A-Za-z0-9_-]*)[^>]*>")
_RECOMMENDED_PLUGINS_RE = re.compile(
    r"^<recommended_plugins\b",
    flags=re.IGNORECASE,
)
# Only wrappers the harnesses themselves inject are stripped. Operators write
# XML-tagged prompts on purpose (`<task>fix X</task>`), so an unknown leading
# tag is user prose and must stay searchable. Grounded in a corpus scan of
# leading tags across recent Claude Code and Codex user messages.
_HARNESS_WRAPPER_TAGS = frozenset(
    {
        "command-args",
        "command-contents",
        "command-message",
        "command-name",
        "cross-session-message",
        "cwd",
        "environment_context",
        "environment_details",
        "goal_context",
        "heartbeat",
        "ide_diagnostics",
        "ide_opened_file",
        "ide_selection",
        "local-command-caveat",
        "local-command-stderr",
        "local-command-stdout",
        "recommended_plugins",
        "scheduled-task",
        "session-start-hook",
        "subagent_notification",
        "system-reminder",
        "task-notification",
        "turn_aborted",
        "user-prompt-submit-hook",
        "user_instructions",
    }
)
# Codex injects repo/global AGENTS.md content as a synthetic user message
# headed "# AGENTS.md instructions for <directory>" or, in other Codex
# versions, the bare header directly followed by an <INSTRUCTIONS> block.
# Requiring one of those continuations keeps operator prose ABOUT the
# instructions ("# AGENTS.md instructions are confusing…") searchable.
_CODEX_AGENTS_INSTRUCTIONS_PREFIX = "# AGENTS.md instructions"


def _is_codex_agents_payload(text: str) -> bool:
    if not text.startswith(_CODEX_AGENTS_INSTRUCTIONS_PREFIX):
        return False
    remainder = text[len(_CODEX_AGENTS_INSTRUCTIONS_PREFIX) :].lstrip()
    # Injections continue with an absolute directory or the wrapper block;
    # prose like "for beginners: …" matches neither.
    return remainder.startswith(("for /", "<INSTRUCTIONS>"))


# Long opaque tokens are not useful transcript prose and are commonly image,
# encrypted-thinking, or other base64 payloads. Standard MIME base64 wraps at
# 76 columns, so remove both continuous tokens and multi-line wrapped runs.
_WRAPPED_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/_=-])"
    r"(?:[A-Za-z0-9+/_-]{4,}[ \t]*\r?\n[ \t]*)+"
    r"[A-Za-z0-9+/_-]{2,}={0,2}"
    r"(?![A-Za-z0-9+/_=-])"
)
_SPACED_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/_=-])"
    r"(?:[A-Za-z0-9+/_-]{16,}[ \t]+)+"
    r"[A-Za-z0-9+/_-]{2,}={0,2}"
    r"(?![A-Za-z0-9+/_=-])"
)
# 64+-character unbroken opaque-charset runs are removed WITHOUT decode
# validation: canonical checks cannot distinguish long identifiers from real
# payloads at that length, and the index deliberately treats such runs
# (including full SHA-256 hex digests in prose) as opaque. Raw search covers
# the forensic case.
_BASE64_BLOB_RE = re.compile(
    r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{64,}={0,2}(?![A-Za-z0-9+/_=-])"
)
_PADDED_BASE64_BLOB_RE = re.compile(
    r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{14,}={1,2}"
    r"(?![A-Za-z0-9+/_=-])"
)
_CLAUDE_SEARCH_BLOCK_TYPES = frozenset({"text"})
_CODEX_SEARCH_BLOCK_TYPES = frozenset({"text", "input_text", "output_text"})
_ENVIRONMENT_CONTEXT_RE = re.compile(
    r"^\s*<environment_context>.*?</environment_context>\s*$",
    flags=re.DOTALL,
)
_CWD_CONTEXT_RE = re.compile(
    r"^\s*<cwd>.*?</cwd>\s*$",
    flags=re.DOTALL,
)
_PRIVATE_MARKERS = ("ccshare:private", "agentus:private", "logpile:private")
_ARG_PATH_KEYS = {
    "file",
    "filepath",
    "file_path",
    "filename",
    "path",
    "target_file",
    "target_path",
    "old_path",
    "new_path",
    "destination",
    "destination_path",
    "include",
    "exclude",
}
_ARG_PATH_LIST_KEYS = {
    "file_paths",
    "files",
    "filenames",
    "paths",
    "targets",
}
_COMMON_FILE_NAMES = {
    "makefile",
    "dockerfile",
    "package.json",
    "package-lock.json",
    "bun.lock",
    "bun.lockb",
    "pnpm-lock.yaml",
    "yarn.lock",
    "tsconfig.json",
    "readme.md",
}
_SHELL_CONTROL_TOKENS = {"&&", "||", "|", ";", ">", ">>", "<", "2>", "2>>", "1>", "1>>"}


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _malformed_record_field(record: dict) -> tuple[str, Any] | None:
    """Return the first field shape that would make a parser record unsafe."""
    for field_name in ("type", "record_type", "timestamp"):
        value = record.get(field_name)
        if field_name in record and not isinstance(value, str):
            return field_name, value
    cwd = record.get("cwd")
    if "cwd" in record and cwd is not None and not isinstance(cwd, str):
        return "cwd", cwd
    record_id = record.get("id")
    if (
        "id" in record
        and record_id is not None
        and not isinstance(record_id, (str, int))
    ):
        return "id", record_id
    for field_name in ("message", "payload"):
        value = record.get(field_name)
        if field_name in record and not isinstance(value, dict):
            return field_name, value

    message = record.get("message")
    if isinstance(message, dict):
        message_id = message.get("id")
        if "id" in message and not isinstance(message_id, (str, int)):
            return "message.id", message_id
        content = message.get("content")
        if "content" in message and not isinstance(content, (str, list, dict)):
            return "message.content", content
        usage = message.get("usage")
        if "usage" in message and not isinstance(usage, dict):
            return "message.usage", usage
        if isinstance(usage, dict):
            for key in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
            ):
                value = usage.get(key)
                if value is None:
                    continue
                try:
                    int(value)
                except (TypeError, ValueError, OverflowError):
                    return f"message.usage.{key}", value
            cache_creation = usage.get("cache_creation")
            if isinstance(cache_creation, dict):
                for key in (
                    "ephemeral_5m_input_tokens",
                    "ephemeral_1h_input_tokens",
                ):
                    value = cache_creation.get(key)
                    if value is None:
                        continue
                    try:
                        int(value)
                    except (TypeError, ValueError, OverflowError):
                        return f"message.usage.cache_creation.{key}", value
        message_model = message.get("model")
        if (
            "model" in message
            and message_model is not None
            and not isinstance(message_model, str)
        ):
            return "message.model", message_model

        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if "type" in block and not isinstance(block_type, str):
                    return "message.content.type", block_type
                for key in ("id", "tool_use_id"):
                    value = block.get(key)
                    if key in block and not isinstance(value, (str, int)):
                        return f"message.content.{key}", value
                if block_type == "tool_use":
                    tool_name = block.get("name")
                    if "name" in block and not isinstance(tool_name, str):
                        return "message.content.name", tool_name

    payload = record.get("payload")
    if isinstance(payload, dict):
        payload_type = payload.get("type")
        if "type" in payload and not isinstance(payload_type, str):
            return "payload.type", payload_type
        for key in ("id", "call_id"):
            value = payload.get(key)
            if key in payload and not isinstance(value, (str, int)):
                return f"payload.{key}", value
        content = payload.get("content")
        if "content" in payload and not isinstance(content, (str, list, dict)):
            return "payload.content", content
        for key in ("cwd", "model", "timestamp"):
            value = payload.get(key)
            if key in payload and value is not None and not isinstance(value, str):
                return f"payload.{key}", value

        source = payload.get("source")
        if isinstance(source, dict):
            subagent = source.get("subagent")
            if isinstance(subagent, dict):
                thread_spawn = subagent.get("thread_spawn")
                if isinstance(thread_spawn, dict):
                    parent_thread_id = thread_spawn.get("parent_thread_id")
                    if (
                        "parent_thread_id" in thread_spawn
                        and parent_thread_id is not None
                        and not isinstance(parent_thread_id, (str, int))
                    ):
                        return (
                            "payload.source.subagent.thread_spawn.parent_thread_id",
                            parent_thread_id,
                        )
                    depth = thread_spawn.get("depth")
                    if depth is not None:
                        try:
                            int(depth)
                        except (TypeError, ValueError, OverflowError):
                            return "payload.source.subagent.thread_spawn.depth", depth

    if isinstance(payload, dict) and payload.get("type") == "token_count":
        info = payload.get("info")
        if "info" in payload and not isinstance(info, dict):
            return "payload.info", info
        if isinstance(info, dict):
            totals = info.get("total_token_usage")
            if "total_token_usage" in info and not isinstance(totals, dict):
                return "payload.info.total_token_usage", totals
            if isinstance(totals, dict):
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                ):
                    value = totals.get(key)
                    if value is None:
                        continue
                    try:
                        int(value)
                    except (TypeError, ValueError, OverflowError):
                        return f"payload.info.total_token_usage.{key}", value

    # Codex function calls occur both as legacy top-level records and as
    # response_item payloads.  _infer_operation calls .lower() on this value,
    # so reject a malformed tool name here instead of letting one record abort
    # parsing the rest of the transcript (or later files in a sync).
    record_type = record.get("type", record.get("record_type", ""))
    function_call = None
    field_prefix = ""
    if record_type == "function_call":
        function_call = record
    elif (
        record_type == "response_item"
        and isinstance(payload, dict)
        and payload.get("type") == "function_call"
    ):
        function_call = payload
        field_prefix = "payload."
    if function_call is not None:
        tool_name = function_call.get("name")
        if "name" in function_call and not isinstance(tool_name, str):
            return f"{field_prefix}name", tool_name
    return None


def _extract_text(content: Any) -> str:
    """Extract plain text from content (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "thinking"):
            if key in content:
                return str(content[key])
        if "content" in content:
            return _extract_text(content["content"])
        return ""
    if isinstance(content, list):
        parts = []
        for block in content:
            text = _extract_text(block)
            if text:
                parts.append(text)
        return " ".join(parts)
    return ""


def strip_harness_preamble(value: str | None) -> str:
    """Match the web app's ``sessionTitle`` harness cleanup exactly.

    Codex can persist synthetic preamble messages before the operator's ask.
    The title UI treats a leading ``recommended_plugins`` payload as empty;
    for other XML-ish wrappers it removes at most four leading blocks (or
    orphan opening tags). Search uses the same contract for goals and first
    user messages so injected harness text is not promoted as session prose.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if _RECOMMENDED_PLUGINS_RE.match(text):
        return ""

    for _ in range(4):
        if not text.startswith("<"):
            break
        match = _HARNESS_OPEN_TAG_RE.match(text)
        if match is None:
            break
        if match.group(1).lower() not in _HARNESS_WRAPPER_TAGS:
            break
        closing = f"</{match.group(1)}>"
        closing_at = text.find(closing)
        if closing_at >= 0:
            text = text[closing_at + len(closing) :].strip()
        else:
            text = text[match.end() :].strip()
    # A codex session whose stored title IS the injected AGENTS.md payload
    # has no operator title; the stored row itself is healed separately by a
    # future token-version reparse.
    if _is_codex_agents_payload(text):
        return ""
    return text


def strip_transcript_harness_preamble(value: str | None) -> str:
    """Remove leading harness blocks while preserving trailing user prose.

    The title contract intentionally treats any value beginning with
    ``recommended_plugins`` as empty. A transcript message can contain that
    wrapper and the real ask in one block, so transcript extraction removes
    the wrapper itself and retains the suffix.
    """
    text = (value or "").strip()
    for _ in range(4):
        if not text.startswith("<"):
            break
        match = _HARNESS_OPEN_TAG_RE.match(text)
        if match is None:
            break
        if match.group(1).lower() not in _HARNESS_WRAPPER_TAGS:
            break
        closing = f"</{match.group(1)}>"
        closing_at = text.find(closing)
        if closing_at >= 0:
            text = text[closing_at + len(closing) :].strip()
        elif match.group(1).lower() == "recommended_plugins":
            # Unlike a generic orphan tag, an unclosed plugin catalog has no
            # trustworthy boundary between injected text and operator prose.
            return ""
        else:
            text = text[match.end() :].strip()
    return text


def _decode_canonical_base64(
    value: str,
    *,
    minimum_length: int,
) -> bytes | None:
    """Decode canonical standard/URL base64, or return ``None``."""
    compact = re.sub(r"\s+", "", value)
    if len(compact) < minimum_length or len(compact) % 4 == 1:
        return None
    standard = compact.replace("-", "+").replace("_", "/")
    padded = standard + ("=" * (-len(standard) % 4))
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) < 12:
        return None
    if base64.b64encode(decoded).decode().rstrip("=") != standard.rstrip("="):
        return None
    return decoded


def _canonical_base64_payload(value: str, *, minimum_length: int) -> bool:
    return _decode_canonical_base64(value, minimum_length=minimum_length) is not None


def _credible_unpadded_base64_tail(prefix: str, complete: str) -> bool:
    """Distinguish a short unpadded tail from ordinary trailing prose.

    When the decoded full-width prefix is text-like, require the candidate
    tail to decode as text-like too. Binary/encrypted prefixes admit any
    canonical tail. This retains words such as ``done`` after an encoded text
    run while still removing unpadded ``eHh4``-style final chunks.
    """
    if len(prefix) % 4:
        return False
    prefix_bytes = _decode_canonical_base64(prefix, minimum_length=64)
    complete_bytes = _decode_canonical_base64(complete, minimum_length=64)
    if prefix_bytes is None or complete_bytes is None:
        return False
    tail = complete_bytes[len(prefix_bytes) :]
    if not tail:
        return False

    def printable_ratio(value: bytes) -> float:
        printable = sum(byte in {9, 10, 13} or 32 <= byte <= 126 for byte in value)
        return printable / len(value)

    return printable_ratio(prefix_bytes) < 0.85 or printable_ratio(tail) >= 0.85


def _remove_wrapped_base64(match: re.Match[str]) -> str:
    """Remove newline-wrapped base64 only when it is shaped like MIME output.

    Encoders wrap at a fixed column, so every line except the last must share
    one width of at least 16 and the last must not exceed it. Ragged runs of
    single-word lines can concatenate into decodable strings by coincidence
    ("first\\nsecond\\n…"), so shape is checked before the canonical decode.
    """
    value = match.group(0)
    lines = [segment.strip() for segment in value.splitlines()]
    widths = {len(segment) for segment in lines[:-1]}
    if len(widths) != 1:
        return value
    width = next(iter(widths))
    # Real encoders wrap at 60+ columns; 24 keeps every observed wrap while
    # sparing coincidentally equal-length word pairs ("misunderstanding\n
    # responsibilities" is uniform at 16).
    if width < 24 or len(lines[-1]) > width:
        return value
    return " " if _canonical_base64_payload(value, minimum_length=32) else value


def _remove_spaced_base64(match: re.Match[str]) -> str:
    """Remove canonical fixed-width base64 chunks separated horizontally."""
    value = match.group(0)
    tokens = list(re.finditer(r"[A-Za-z0-9+/_-]+={0,2}", value))
    removals: list[tuple[int, int]] = []
    start = 0
    while start < len(tokens) - 1:
        width = len(tokens[start].group(0).rstrip("="))
        if width < 16:
            start += 1
            continue
        equal_end = start + 1
        while (
            equal_end < len(tokens)
            and len(tokens[equal_end].group(0).rstrip("=")) == width
            and not tokens[equal_end - 1].group(0).endswith("=")
        ):
            equal_end += 1

        candidate_end: int | None = None
        if equal_end < len(tokens):
            short = tokens[equal_end].group(0)
            short_width = len(short.rstrip("="))
            if 2 <= short_width <= width:
                prefix = "".join(token.group(0) for token in tokens[start:equal_end])
                candidate = "".join(
                    token.group(0) for token in tokens[start : equal_end + 1]
                )
                tail_is_credible = short.endswith("=") or (
                    _credible_unpadded_base64_tail(prefix, candidate)
                )
                if tail_is_credible and _canonical_base64_payload(
                    candidate,
                    minimum_length=64,
                ):
                    candidate_end = equal_end + 1
        if candidate_end is None and equal_end - start >= 2:
            candidate = "".join(token.group(0) for token in tokens[start:equal_end])
            if _canonical_base64_payload(candidate, minimum_length=64):
                candidate_end = equal_end
            elif equal_end - start >= 3 and tokens[equal_end - 1].group(0).endswith(
                "="
            ):
                # A malformed padded terminal must not make us retry every
                # suffix of a huge equal-width run. Remove the canonical
                # full-chunk prefix if possible, then skip the terminal once.
                prefix_end = equal_end - 1
                prefix = "".join(token.group(0) for token in tokens[start:prefix_end])
                if _canonical_base64_payload(prefix, minimum_length=64):
                    candidate_end = prefix_end

        if candidate_end is None:
            start = max(start + 1, equal_end)
            continue
        removals.append((tokens[start].start(), tokens[candidate_end - 1].end()))
        start = candidate_end

    if not removals:
        return value
    pieces: list[str] = []
    cursor = 0
    for start_at, end_at in removals:
        pieces.extend((value[cursor:start_at], " "))
        cursor = end_at
    pieces.append(value[cursor:])
    return "".join(pieces)


def _remove_padded_base64(match: re.Match[str]) -> str:
    value = match.group(0)
    return " " if _canonical_base64_payload(value, minimum_length=16) else value


def clean_search_text(
    value: str | None,
    *,
    strip_preamble: bool = False,
) -> str:
    """Return readable index text with harness/base64 payloads removed."""
    text = strip_harness_preamble(value) if strip_preamble else (value or "").strip()
    if not text:
        return ""
    text = _WRAPPED_BASE64_RE.sub(_remove_wrapped_base64, text)
    text = _SPACED_BASE64_RE.sub(_remove_spaced_base64, text)
    text = _PADDED_BASE64_BLOB_RE.sub(_remove_padded_base64, text)
    text = _BASE64_BLOB_RE.sub(" ", text)
    return " ".join(text.split())


def _load_jsonl(
    path: Path,
    *,
    stats: JsonlLoadStats | None = None,
) -> list[dict]:
    """Load object records and isolate malformed lines from the rest of a file."""
    load_stats = stats if stats is not None else JsonlLoadStats()
    records: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    load_stats.invalid_json_lines += 1
                    continue
                except Exception as exc:
                    # A pathological record (for example excessive nesting)
                    # must not discard the valid records around it.
                    load_stats.count_exception(exc)
                    continue
                if not isinstance(record, dict):
                    load_stats.count_record_type(record)
                    continue
                try:
                    malformed_field = _malformed_record_field(record)
                except Exception as exc:
                    load_stats.count_exception(exc)
                    continue
                if malformed_field is not None:
                    load_stats.count_malformed_field(*malformed_field)
                    continue
                records.append(record)
    except OSError as exc:
        load_stats.io_errors += 1
        # A read that fails after yielding records is not a valid snapshot.
        # Discard the partial load so callers cannot commit a truncated
        # SessionInfo while a live transcript is being rotated or rewritten.
        records.clear()
        logger.warning("Could not read JSONL file %s: %s", path, exc)

    if load_stats.malformed_record_count:
        logger.warning(
            "Skipped %d malformed JSONL record(s) in %s: %s",
            load_stats.malformed_record_count,
            path,
            load_stats.as_dict(),
        )
    return records


def _bounded_lines(
    stream: TextIO,
    max_line_chars: int,
    stats: JsonlLoadStats,
) -> Iterator[str]:
    """Yield lines without ever materializing one longer than the cap.

    ``for line in stream`` builds the whole line first, so a single
    multi-gigabyte record would be held in memory just to be discarded.
    Reading fixed chunks keeps peak memory at cap + chunk size; oversized
    lines are counted and skipped.
    """
    pending: list[str] = []
    pending_chars = 0
    skipping = False
    while True:
        chunk = stream.read(1 << 20)
        if not chunk:
            break
        while chunk:
            newline_at = chunk.find("\n")
            if newline_at < 0:
                if not skipping:
                    pending_chars += len(chunk)
                    if pending_chars > max_line_chars:
                        skipping = True
                        pending.clear()
                    else:
                        pending.append(chunk)
                break
            head, chunk = chunk[:newline_at], chunk[newline_at + 1 :]
            if skipping:
                skipping = False
                stats.malformed_fields["line:oversized"] = (
                    stats.malformed_fields.get("line:oversized", 0) + 1
                )
            else:
                pending_chars += len(head)
                if pending_chars > max_line_chars:
                    stats.malformed_fields["line:oversized"] = (
                        stats.malformed_fields.get("line:oversized", 0) + 1
                    )
                else:
                    pending.append(head)
                    yield "".join(pending)
            pending.clear()
            pending_chars = 0
    if skipping or pending_chars > max_line_chars:
        stats.malformed_fields["line:oversized"] = (
            stats.malformed_fields.get("line:oversized", 0) + 1
        )
    elif pending:
        yield "".join(pending)


def _iter_jsonl(
    path: Path | TextIO,
    *,
    stats: JsonlLoadStats | None = None,
    report_malformed: bool = True,
    max_line_chars: int | None = None,
) -> Iterator[dict]:
    """Yield validated JSONL objects without retaining the transcript.

    Callers that build durable state must check ``stats.io_errors`` after the
    iterator is exhausted.  A file that rotates during a read may already
    have yielded records, but the non-zero error count lets the caller discard
    its compact partial state instead of committing a truncated session.

    ``_load_jsonl`` remains as a compatibility helper for focused loader
    diagnostics.  Ingestion and transcript rendering use this iterator so a
    multi-gigabyte JSONL file is never materialized as a list of dictionaries.
    """
    load_stats = stats if stats is not None else JsonlLoadStats()
    label = getattr(path, "name", path)
    stream_context = (
        nullcontext(path)
        if hasattr(path, "read")
        else open(path, encoding="utf-8", errors="replace")  # noqa: SIM115 - entered via `with stream_context` below
    )
    try:
        if hasattr(path, "seek"):
            path.seek(0)
        with stream_context as f:
            lines: Iterator[str] = (
                _bounded_lines(f, max_line_chars, load_stats)
                if max_line_chars is not None
                else f
            )
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    load_stats.invalid_json_lines += 1
                    continue
                except Exception as exc:
                    load_stats.count_exception(exc)
                    continue
                if not isinstance(record, dict):
                    load_stats.count_record_type(record)
                    continue
                try:
                    malformed_field = _malformed_record_field(record)
                except Exception as exc:
                    load_stats.count_exception(exc)
                    continue
                if malformed_field is not None:
                    load_stats.count_malformed_field(*malformed_field)
                    continue
                yield record
    except OSError as exc:
        load_stats.io_errors += 1
        logger.warning("Could not read JSONL file %s: %s", label, exc)
    finally:
        if report_malformed and load_stats.malformed_record_count:
            logger.warning(
                "Skipped %d malformed JSONL record(s) in %s: %s",
                load_stats.malformed_record_count,
                label,
                load_stats.as_dict(),
            )


def _normalize_codex_record(record: dict) -> tuple[str, dict, str | None]:
    """Return (record_type, payload, timestamp) for old and new Codex formats."""
    record_type = record.get("type", record.get("record_type", ""))
    timestamp = record.get("timestamp")

    if record_type == "response_item" and isinstance(record.get("payload"), dict):
        payload = record["payload"]
        return payload.get("type", ""), payload, timestamp

    if record_type in {"session_meta", "event_msg", "turn_context"} and isinstance(
        record.get("payload"), dict
    ):
        return record_type, record["payload"], timestamp

    return record_type, record, timestamp


def _load_tool_args(arguments: Any) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str):
        return {}
    try:
        loaded = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _extract_codex_token_totals(
    record_type: str, payload: dict
) -> tuple[int, int, int, int] | None:
    if record_type != "event_msg":
        return None
    if payload.get("type") != "token_count":
        return None
    info = payload.get("info", {})
    if not isinstance(info, dict):
        return None
    # Rate-limit-only token_count events omit total_token_usage. Treating that
    # absence as an all-zero vector would manufacture a billing-epoch reset.
    if "total_token_usage" not in info:
        return None
    totals = info.get("total_token_usage")
    if not isinstance(totals, dict):
        return None
    if not any(
        key in totals
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
    ):
        return None
    input_tokens = int(totals.get("input_tokens", 0) or 0)
    cached_input_tokens = int(totals.get("cached_input_tokens", 0) or 0)
    output_tokens = int(totals.get("output_tokens", 0) or 0)
    reasoning_output_tokens = int(totals.get("reasoning_output_tokens", 0) or 0)
    return input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens


def _is_explicit_codex_counter_reset(record_type: str, payload: dict) -> bool:
    """Whether this token event explicitly resets every billed counter."""
    if record_type != "event_msg" or payload.get("type") != "token_count":
        return False
    info = payload.get("info")
    if not isinstance(info, dict):
        return False
    totals = info.get("total_token_usage")
    if not isinstance(totals, dict):
        return False
    # Input/cached/output are present in every real cumulative snapshot. The
    # reasoning component is optional in older records and is zero when absent.
    required = ("input_tokens", "cached_input_tokens", "output_tokens")
    if not all(key in totals for key in required):
        return False
    try:
        return all(
            int(totals.get(key, 0) or 0) == 0
            for key in (*required, "reasoning_output_tokens")
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _day_of(timestamp: str | None) -> str | None:
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    value = timestamp.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).date().isoformat()


def _daily_bucket(daily: dict, timestamp: str | None) -> DailyUsage | None:
    day = _day_of(timestamp)
    if day is None:
        return None
    bucket = daily.get(day)
    if bucket is None:
        bucket = daily[day] = DailyUsage(day=day)
    return bucket


def _sorted_daily(daily: dict) -> list[DailyUsage]:
    return [daily[day] for day in sorted(daily)]


def _string_id(value: Any) -> str | None:
    if isinstance(value, (str, int)) and str(value):
        return str(value)
    return None


def _timestamp_epoch_second(timestamp: str | None) -> int | None:
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    value = timestamp.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _started_at_epoch_second(value: Any) -> int | None:
    try:
        started_at = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    # Codex currently emits integer epoch seconds, but accept epoch
    # milliseconds as well so a representation change does not revive the
    # timestamp-burst heuristic.
    if abs(started_at) >= 100_000_000_000:
        started_at /= 1000
    return int(started_at)


def _codex_replay_window(records: list) -> tuple[int, int]:
    """Return the structurally copied prefix of a Codex fork snapshot.

    A copied snapshot starts with the new leaf ``session_meta`` followed by
    the copied parent's ``session_meta``.  The copied meta's id exactly
    matches the leaf's ``forked_from_id``.  Replayed ``task_started`` records
    retain their original ``started_at`` value while their outer timestamps
    are re-stamped; the first task whose two clocks agree is therefore the
    leaf-native boundary. This remains reliable when serialization of a
    large prefix spans many wall-clock seconds.

    Fork metadata without the matching second meta is not enough: current
    Codex files can be fresh child threads with no copied prefix.  If a
    structurally identified snapshot is still being written and has no
    native task boundary yet, all records after the leaf meta are inherited.
    """
    if len(records) < 2:
        return 0, 0
    first_type, first_payload, _ = _normalize_codex_record(records[0])
    second_type, second_payload, _ = _normalize_codex_record(records[1])
    if first_type != "session_meta" or second_type != "session_meta":
        return 0, 0
    forked_from_id = _string_id(first_payload.get("forked_from_id"))
    copied_thread_id = _string_id(second_payload.get("id"))
    if not forked_from_id or copied_thread_id != forked_from_id:
        return 0, 0

    for index, record in enumerate(records[2:], start=2):
        record_type, payload, timestamp = _normalize_codex_record(record)
        if record_type != "event_msg" or payload.get("type") != "task_started":
            continue
        started_at = _started_at_epoch_second(payload.get("started_at"))
        record_second = _timestamp_epoch_second(timestamp)
        if started_at is not None and started_at == record_second:
            return 1, index
    return 1, len(records)


def _extract_command(arguments: dict) -> str | None:
    raw_cmd = arguments.get("command") or arguments.get("cmd")
    if isinstance(raw_cmd, list):
        if len(raw_cmd) >= 3 and raw_cmd[1] == "-lc":
            return str(raw_cmd[-1])
        return " ".join(str(c) for c in raw_cmd)
    if isinstance(raw_cmd, str):
        return raw_cmd
    return None


def _infer_operation(tool_name: str | None, command: str | None = None) -> str:
    lowered = (tool_name or "").lower()
    if any(
        part in lowered
        for part in (
            "edit",
            "write",
            "replace",
            "patch",
            "apply",
            "create",
            "delete",
            "rename",
            "move",
        )
    ):
        return "write"
    if any(part in lowered for part in ("read", "open", "view", "load", "cat")):
        return "read"
    if any(
        part in lowered
        for part in ("search", "find", "grep", "glob", "list", "ls", "scan")
    ):
        return "search"
    if any(
        part in lowered for part in ("bash", "shell", "exec", "terminal", "command")
    ):
        try:
            tokens = shlex.split(command or "")
        except ValueError:
            tokens = (command or "").split()
        cmd_name = Path(tokens[0]).name if tokens else ""
        if cmd_name in {"rg", "grep", "fd", "find", "ls", "tree", "git"}:
            return "search"
        if cmd_name in {"cat", "bat", "sed", "head", "tail"}:
            return "read"
        if cmd_name in {
            "mv",
            "cp",
            "rm",
            "touch",
            "mkdir",
            "tee",
            "apply_patch",
            "prettier",
            "black",
            "isort",
            "gofmt",
            "rustfmt",
            "clang-format",
            "stylua",
        }:
            return "write"
        lowered_command = (command or "").lower()
        if "ruff format" in lowered_command or "biome format" in lowered_command:
            return "write"
        return "command"
    return "other"


def _key_suggests_path(key: str | None) -> bool:
    lowered = (key or "").strip().lower()
    return (
        lowered in _ARG_PATH_KEYS
        or lowered in _ARG_PATH_LIST_KEYS
        or lowered.endswith(("_path", "_file", "_files"))
    )


def _clean_path_candidate(value: str) -> str:
    candidate = str(value).strip().strip("\"'")
    candidate = candidate.rstrip(",;")
    candidate = candidate.removeprefix("file://")
    line_match = re.match(r"^(.*?)(?::\d+(?::\d+)?)$", candidate)
    if line_match:
        base = line_match.group(1)
        if "/" in base or "\\" in base or "." in Path(base).name:
            candidate = base
    return candidate


def _looks_like_path(value: str) -> bool:
    candidate = _clean_path_candidate(value)
    if not candidate or "\n" in candidate or "\r" in candidate:
        return False
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", candidate):
        return False
    if candidate.startswith("-"):
        return False
    if candidate in {".", ".."}:
        return False
    if "/" in candidate or "\\" in candidate or candidate.startswith("~"):
        return True
    if any(ch in candidate for ch in "*?[]"):
        return True
    leaf = Path(candidate).name
    if leaf.lower() in _COMMON_FILE_NAMES:
        return True
    if re.search(r"\.[A-Za-z0-9]{1,10}$", leaf):
        return True
    return candidate.startswith(".") and len(candidate) > 1


def _extract_argument_paths(arguments: Any, parent_key: str | None = None) -> list[str]:
    paths: list[str] = []
    if isinstance(arguments, dict):
        for key, value in arguments.items():
            lowered = str(key).strip().lower()
            if isinstance(value, str):
                if _key_suggests_path(lowered) and _looks_like_path(value):
                    paths.append(_clean_path_candidate(value))
            elif isinstance(value, list):
                if lowered in _ARG_PATH_LIST_KEYS:
                    for item in value:
                        if isinstance(item, str) and _looks_like_path(item):
                            paths.append(_clean_path_candidate(item))
                        elif isinstance(item, dict):
                            paths.extend(_extract_argument_paths(item, lowered))
                else:
                    for item in value:
                        if isinstance(item, dict):
                            paths.extend(_extract_argument_paths(item, lowered))
            elif isinstance(value, dict):
                paths.extend(_extract_argument_paths(value, lowered))
    elif isinstance(arguments, list) and parent_key in _ARG_PATH_LIST_KEYS:
        for item in arguments:
            if isinstance(item, str) and _looks_like_path(item):
                paths.append(_clean_path_candidate(item))
    return paths


def _extract_command_paths(command: str | None) -> list[str]:
    if not command:
        return []
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    paths: list[str] = []
    for token in tokens[1:]:
        cleaned = _clean_path_candidate(token)
        if not cleaned or cleaned in _SHELL_CONTROL_TOKENS or cleaned.startswith("-"):
            continue
        if _looks_like_path(cleaned):
            paths.append(cleaned)
    return paths


def _unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _display_path(path: Path) -> str:
    parts = [part for part in path.parts if part not in ("/", "\\")]
    if len(parts) >= 2:
        return f"…/{parts[-2]}/{parts[-1]}"
    if parts:
        return f"…/{parts[-1]}"
    return str(path)


def _safe_expanduser(path: Path) -> Path:
    try:
        return path.expanduser()
    except RuntimeError:
        return path


def _normalize_session_path(
    candidate: str, workspace_root: str | None
) -> tuple[str, str | None, str] | None:
    raw_path = _clean_path_candidate(candidate)
    if not _looks_like_path(raw_path):
        return None

    root = (
        _safe_expanduser(Path(workspace_root))
        if workspace_root and workspace_root != "unknown"
        else None
    )
    path = _safe_expanduser(Path(raw_path))
    if not path.is_absolute() and root is not None:
        path = root / raw_path

    normalized_path = str(path)
    relative_path: str | None = None
    if root is not None and path.is_absolute():
        try:
            relative_path = str(path.relative_to(root))
        except ValueError:
            relative_path = None

    if relative_path and relative_path not in {"", "."}:
        display_path = relative_path
    elif path.is_absolute():
        display_path = _display_path(path)
    else:
        display_path = raw_path

    return normalized_path, relative_path, display_path


def _build_session_paths(
    tool_calls: list[ToolCall], workspace_root: str | None
) -> list[SessionPath]:
    session_paths: list[SessionPath] = []
    for tool_call in tool_calls:
        for source_name, candidates in (
            ("tool_input", tool_call.input_paths),
            ("command", tool_call.command_paths),
        ):
            for candidate in _unique_preserve_order(candidates):
                normalized = _normalize_session_path(candidate, workspace_root)
                if not normalized:
                    continue
                normalized_path, relative_path, display_path = normalized
                session_paths.append(
                    SessionPath(
                        raw_path=candidate,
                        normalized_path=normalized_path,
                        relative_path=relative_path,
                        display_path=display_path,
                        operation=tool_call.operation,
                        source=source_name,
                        tool_name=tool_call.tool_name,
                        timestamp=tool_call.timestamp,
                    )
                )
    return session_paths


def _parse_tool_output(output_raw: Any) -> tuple[str, bool]:
    is_error = False
    parsed = None

    if isinstance(output_raw, str):
        try:
            parsed = json.loads(output_raw)
        except json.JSONDecodeError:
            parsed = None
    elif isinstance(output_raw, (dict, list)):
        parsed = output_raw

    if isinstance(parsed, dict):
        display = parsed.get("output", parsed.get("content", parsed))
        meta = parsed.get("metadata", {})
        if isinstance(meta, dict):
            exit_code = meta.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                is_error = True
        if parsed.get("is_error"):
            is_error = True
    elif parsed is not None:
        display = parsed
    else:
        display = output_raw

    display_text = str(display)
    exit_match = re.search(r"Process exited with code (\d+)", display_text)
    if exit_match and int(exit_match.group(1)) != 0:
        is_error = True

    return display_text, is_error


def _clean_codex_user_text(text: str) -> str:
    clean = _USER_CONTEXT_PREAMBLE_RE.sub("", text).strip()
    if _ENVIRONMENT_CONTEXT_RE.fullmatch(clean) or _CWD_CONTEXT_RE.fullmatch(clean):
        return ""
    return clean


class SearchTranscriptReadError(RuntimeError):
    """A transcript could not be read completely for search indexing."""


# No legitimate prose line approaches this; a single-record blob larger than
# the cap is skipped (and counted) instead of being materialized and decoded.
_SEARCH_MAX_LINE_CHARS = 64 * 1024 * 1024


def _iter_search_content_text(
    content: Any,
    *,
    block_types: frozenset[str],
    allow_untyped_text: bool = False,
) -> Iterator[str]:
    """Yield only explicitly textual message content without recursion."""
    if isinstance(content, str):
        yield content
        return
    if isinstance(content, dict):
        block_type = content.get("type")
        is_legacy_text = (
            allow_untyped_text and block_type is None and set(content) == {"text"}
        )
        if (block_type in block_types or is_legacy_text) and isinstance(
            content.get("text"), str
        ):
            yield content["text"]
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        # Early Codex JSONL used exact bare {"text": ...} message blocks.
        # Extra tool/reasoning/image keys make an untyped block ineligible.
        is_legacy_text = (
            allow_untyped_text and block_type is None and set(block) == {"text"}
        )
        if block_type not in block_types and not is_legacy_text:
            continue
        text = block.get("text")
        if isinstance(text, str):
            yield text


def iter_session_search_text(
    path: Path | TextIO,
    source: str,
) -> Iterator[tuple[str, str]]:
    """Stream searchable ``(role, text)`` pairs from one transcript.

    This intentionally does not call :func:`_extract_text`, whose display and
    parser semantics include thinking and recursively nested content. Search
    accepts only user/assistant message strings and explicitly typed text
    blocks. Tool calls/results, reasoning, encrypted content, images, and all
    other block types never reach the index.
    """
    if source not in {"claudecode", "codex"}:
        raise ValueError(f"Unsupported transcript source: {source}")

    load_stats = JsonlLoadStats()
    for record in _iter_jsonl(
        path,
        stats=load_stats,
        max_line_chars=_SEARCH_MAX_LINE_CHARS,
    ):
        if source == "claudecode":
            role = record.get("type", "")
            if role not in {"user", "assistant"}:
                continue
            # Harness-injected records (caveats, command echoes) carry
            # isMeta; they are not operator prose.
            if record.get("isMeta"):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content", "")
            # Text blocks riding a tool-result user record are harness
            # attachments (system reminders, hook output), not something the
            # operator typed. Real user turns never share a record with
            # tool_result blocks.
            if (
                role == "user"
                and isinstance(content, list)
                and any(
                    isinstance(block, dict) and block.get("type") == "tool_result"
                    for block in content
                )
            ):
                continue
            block_types = _CLAUDE_SEARCH_BLOCK_TYPES
        else:
            record_type, payload, _timestamp = _normalize_codex_record(record)
            if record_type != "message":
                continue
            role = payload.get("role", "")
            if role not in {"user", "assistant"}:
                continue
            content = payload.get("content", "")
            block_types = _CODEX_SEARCH_BLOCK_TYPES

        for raw_text in _iter_search_content_text(
            content,
            block_types=block_types,
            allow_untyped_text=source == "codex",
        ):
            if role == "user":
                if source == "codex":
                    raw_text = _clean_codex_user_text(raw_text)
                raw_text = strip_transcript_harness_preamble(raw_text)
                # Check AFTER wrapper stripping so a payload hiding behind a
                # leading harness wrapper is still recognized.
                if source == "codex" and _is_codex_agents_payload(raw_text):
                    continue
                text = clean_search_text(raw_text)
            else:
                text = clean_search_text(raw_text)
            if text:
                yield role, text

    if load_stats.io_errors:
        label = getattr(path, "name", path)
        raise SearchTranscriptReadError(
            f"Could not read complete transcript for search indexing: {label}"
        )


def _extract_codex_project(records: list[dict]) -> str:
    for record in records:
        record_type, payload, _ = _normalize_codex_record(record)
        if record_type in {"session_meta", "turn_context"} and payload.get("cwd"):
            return str(payload["cwd"])
        if record_type == "message" and payload.get("role") == "user":
            text = _extract_text(payload.get("content", []))
            match = re.search(r"<cwd>(.*?)</cwd>", text, flags=re.DOTALL)
            if match:
                return match.group(1).strip()
    return "unknown"


def _private_marker(records: list) -> str | None:
    """Return the first explicit privacy marker in a session, if present."""
    for record in records:
        record_type, payload, _ = _normalize_codex_record(record)
        texts = []

        if record_type == "message":
            texts.append(_extract_text(payload.get("content", "")))
        else:
            msg = record.get("message", {})
            content = (
                msg.get("content")
                or record.get("content")
                or payload.get("content")
                or ""
            )
            texts.append(_extract_text(content))

        for key in ("instructions", "base_instructions"):
            texts.append(_extract_text(payload.get(key)))
            texts.append(_extract_text(record.get(key)))

        for marker in _PRIVATE_MARKERS:
            if any(marker in text for text in texts if text):
                return marker
    return None


_DAILY_RECONCILE_FIELDS = (
    "total_input_tokens",
    "total_output_tokens",
    "fresh_input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
    "cache_creation_unknown_input_tokens",
    "reasoning_output_tokens",
    "user_message_count",
    "assistant_message_count",
    "tool_call_count",
)


def _fallback_usage_day(path: Path, records: list[dict]) -> str:
    """Choose the deterministic day for usage lacking an event timestamp."""
    for record in records:
        day = _day_of(record.get("timestamp"))
        if day is not None:
            return day
    try:
        modified = path.stat().st_mtime
    except OSError:
        # The file was readable moments earlier, so this is only a final
        # deterministic guard for a concurrent rotation.
        return "1970-01-01"
    return datetime.fromtimestamp(modified, tz=UTC).date().isoformat()


def _reconcile_daily_usage(
    daily: dict[str, DailyUsage],
    fallback_day: str,
    totals: dict[str, int],
) -> None:
    """Make every per-day token/count component equal its session total.

    Normally only positive residuals occur: an otherwise valid usage record
    had no timestamp.  The defensive negative branch removes excess from the
    latest buckets without allowing negative rows, preserving the invariant
    even if a malformed retry produces duplicate attribution.
    """
    fallback_bucket: DailyUsage | None = None
    for field_name in _DAILY_RECONCILE_FIELDS:
        target = max(0, int(totals.get(field_name, 0) or 0))
        current = sum(
            int(getattr(bucket, field_name, 0) or 0) for bucket in daily.values()
        )
        delta = target - current
        if delta > 0:
            if fallback_bucket is None:
                fallback_bucket = daily.get(fallback_day)
                if fallback_bucket is None:
                    fallback_bucket = daily[fallback_day] = DailyUsage(day=fallback_day)
            setattr(
                fallback_bucket,
                field_name,
                int(getattr(fallback_bucket, field_name, 0) or 0) + delta,
            )
            fallback_bucket.approximated = True
        elif delta < 0:
            excess = -delta
            for day in sorted(daily, reverse=True):
                bucket = daily[day]
                value = int(getattr(bucket, field_name, 0) or 0)
                removed = min(value, excess)
                if removed:
                    setattr(bucket, field_name, value - removed)
                    bucket.approximated = True
                    excess -= removed
                if excess == 0:
                    break


_CLAUDE_USAGE_TUPLE_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def _usage_tuple(usage: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(usage, dict):
        return None
    values: list[int] = []
    for field_name in _CLAUDE_USAGE_TUPLE_FIELDS:
        try:
            values.append(int(usage.get(field_name, 0) or 0))
        except (TypeError, ValueError, OverflowError):
            return None
    return values[0], values[1], values[2], values[3]


def _claude_cache_creation_split(usage: dict) -> tuple[int, int, int]:
    """Return a lossless (5m, 1h, unknown) cache-creation split.

    Fallback records can contain several usage iterations.  Only an
    iteration whose complete usage tuple matches the top-level totals can
    explain that record; the last exact match is authoritative for its cache
    lifetime breakdown.  Missing or contradictory breakdowns remain explicit
    unknown usage instead of being silently labeled 5m.
    """
    total = max(0, int(usage.get("cache_creation_input_tokens", 0) or 0))
    split_source = usage
    top_level_tuple = _usage_tuple(usage)
    iterations = usage.get("iterations")
    if top_level_tuple is not None and isinstance(iterations, list):
        for iteration in iterations:
            if _usage_tuple(iteration) == top_level_tuple:
                split_source = iteration

    breakdown = split_source.get("cache_creation")
    if not isinstance(breakdown, dict):
        return 0, 0, total
    try:
        five_minute = int(breakdown.get("ephemeral_5m_input_tokens", 0) or 0)
        one_hour = int(breakdown.get("ephemeral_1h_input_tokens", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0, 0, total
    if five_minute < 0 or one_hour < 0 or five_minute + one_hour > total:
        return 0, 0, total
    return five_minute, one_hour, total - five_minute - one_hour


def _claude_session_identity(
    path: Path,
    records: list[dict],
) -> tuple[str, str, str | None, str | None, int]:
    """Return canonical id, raw thread id, raw/canonical parent, and depth."""
    raw_session_id = None
    agent_id = None
    sidechain_flag = False
    for record in records:
        if raw_session_id is None:
            raw_session_id = _string_id(record.get("sessionId"))
        if agent_id is None:
            agent_id = _string_id(record.get("agentId"))
        sidechain_flag = sidechain_flag or record.get("isSidechain") is True

    return _claude_session_identity_from_observations(
        path,
        raw_session_id=raw_session_id,
        agent_id=agent_id,
        sidechain_flag=sidechain_flag,
    )


def _claude_session_identity_from_observations(
    path: Path,
    *,
    raw_session_id: str | None,
    agent_id: str | None,
    sidechain_flag: bool,
) -> tuple[str, str, str | None, str | None, int]:
    """Resolve Claude identity from compact fields gathered while streaming."""

    parts = path.parts
    subagent_indices = [
        index for index, part in enumerate(parts) if part == "subagents"
    ]
    path_is_subagent = bool(subagent_indices)
    root_from_path = None
    if subagent_indices and subagent_indices[0] > 0:
        root_from_path = parts[subagent_indices[0] - 1]
    if agent_id is None and path.stem.startswith("agent-"):
        agent_id = path.stem[len("agent-") :]

    if sidechain_flag or path_is_subagent or agent_id is not None:
        raw_agent_id = agent_id or path.stem
        canonical_id = (
            raw_agent_id
            if raw_agent_id.startswith("agent-")
            else f"agent-{raw_agent_id}"
        )
        parent_thread_id = raw_session_id or root_from_path
        depth = max(1, len(subagent_indices))
        return canonical_id, raw_agent_id, parent_thread_id, parent_thread_id, depth

    thread_id = raw_session_id or path.stem
    return path.stem, thread_id, None, None, 0


def _daily_state(daily: dict[str, DailyUsage]) -> list[dict]:
    return [asdict(bucket) for bucket in _sorted_daily(daily)]


def _daily_from_state(rows: list[dict]) -> dict[str, DailyUsage]:
    return {row["day"]: DailyUsage(**row) for row in rows}


class _ClaudeStream:
    """Left fold over one Claude Code transcript's records.

    Cardinality-dependent state lives in the spool; the rest is the handful
    of scalars in ``state()``. A persisted spool plus those scalars resumes
    the fold after the last record a previous sync consumed, and folding the
    remaining records gives exactly what one pass over the whole file gives.
    """

    _SCALARS = (
        "record_count",
        "private_marker",
        "raw_session_id",
        "agent_id",
        "sidechain_flag",
        "project",
        "fallback_day",
        "first_timestamp",
        "last_timestamp",
        "user_message_count",
        "first_user_message",
        "error_count",
        "model",
    )

    def __init__(self, spool: _ParseSpool, state: dict | None = None) -> None:
        self.spool = spool
        self.record_count = 0
        self.private_marker = None
        self.raw_session_id = None
        self.agent_id = None
        self.sidechain_flag = False
        self.project = "unknown"
        self.fallback_day = None
        self.first_timestamp = None
        self.last_timestamp = None
        self.user_message_count = 0
        self.first_user_message = ""
        self.error_count = 0
        self.model = None
        # Only record-driven counts (user messages, tool calls) accumulate
        # here; message token buckets are derived from the spool at finish.
        self.daily: dict[str, DailyUsage] = {}
        if state is not None:
            for name in self._SCALARS:
                setattr(self, name, state[name])
            self.daily = _daily_from_state(state["daily"])

    def state(self) -> dict:
        data = {name: getattr(self, name) for name in self._SCALARS}
        data["daily"] = _daily_state(self.daily)
        return data

    def feed(self, r: dict) -> None:
        spool = self.spool
        self.record_count += 1
        if self.private_marker is None:
            self.private_marker = _private_marker((r,))
        if self.raw_session_id is None:
            self.raw_session_id = _string_id(r.get("sessionId"))
        if self.agent_id is None:
            self.agent_id = _string_id(r.get("agentId"))
        self.sidechain_flag = self.sidechain_flag or r.get("isSidechain") is True
        if self.project == "unknown" and r.get("cwd"):
            self.project = r["cwd"]
        if self.fallback_day is None:
            self.fallback_day = _day_of(r.get("timestamp"))

        rtype = r.get("type", "")
        ts = r.get("timestamp")
        if ts:
            if not self.first_timestamp:
                self.first_timestamp = ts
            self.last_timestamp = ts

        if rtype == "user":
            msg = r.get("message", {})
            content = msg.get("content", "")

            # Tool results are user-role but not user messages
            if isinstance(content, list):
                has_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if has_tool_result:
                    # Count errors
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                        ):
                            is_error = bool(block.get("is_error"))
                            if is_error:
                                self.error_count += 1
                            tool_use_id = block.get("tool_use_id")
                            if tool_use_id:
                                spool.set_tool_result(tool_use_id, is_error)
                    return

            text = " ".join(
                _iter_search_content_text(
                    content,
                    block_types=_CLAUDE_SEARCH_BLOCK_TYPES,
                )
            )
            if text.strip():
                self.user_message_count += 1
                # Harness-injected records (isMeta: caveats, command echoes)
                # keep their count semantics but must not become the title.
                if not self.first_user_message and not r.get("isMeta"):
                    self.first_user_message = text.strip()[:500]
                bucket = _daily_bucket(self.daily, ts)
                if bucket is not None:
                    bucket.user_message_count += 1

        elif rtype == "assistant":
            msg = r.get("message", {})
            msg_id = msg.get("id")
            usage = msg.get("usage", {})
            fresh_inp = int(usage.get("input_tokens", 0) or 0)
            cached_inp = int(usage.get("cache_read_input_tokens", 0) or 0)
            # Cache writes are billed input (at a premium) and were ~1B
            # tokens/month as of Jul 2026 — capture them, split 5m/1h when
            # the breakdown is present (they bill at different rates).
            cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
            cw5, cw1, cw_unknown = _claude_cache_creation_split(usage)
            out = int(usage.get("output_tokens", 0) or 0)
            mdl = msg.get("model")

            if msg_id:
                spool.put_message(
                    str(msg_id),
                    fresh_input=fresh_inp,
                    cached_input=cached_inp,
                    cache_creation=cache_creation,
                    cache_creation_5m=cw5,
                    cache_creation_1h=cw1,
                    cache_creation_unknown=cw_unknown,
                    output=out,
                    model=mdl,
                    timestamp=ts,
                    request_id=r.get("requestId"),
                    uuid=r.get("uuid"),
                )
                if mdl and not self.model:
                    self.model = mdl

            # Extract tool calls from content blocks
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_name = block.get("name", "unknown")
                        tool_use_id = block.get("id")
                        inp_data = block.get("input", {})
                        cmd = None
                        if isinstance(inp_data, dict):
                            cmd = inp_data.get("command") or inp_data.get("cmd")
                            if isinstance(cmd, list):
                                cmd = " ".join(str(c) for c in cmd)
                        input_paths = _extract_argument_paths(inp_data)
                        command_paths = _extract_command_paths(
                            str(cmd) if cmd else None
                        )
                        spool.append_tool_call(
                            ToolCall(
                                tool_name=tool_name,
                                command=str(cmd)[:500] if cmd else None,
                                timestamp=ts,
                                operation=_infer_operation(
                                    tool_name, str(cmd) if cmd else None
                                ),
                                input_paths=input_paths,
                                command_paths=command_paths,
                                call_id=tool_use_id,
                            )
                        )
                        bucket = _daily_bucket(self.daily, ts)
                        if bucket is not None:
                            bucket.tool_call_count += 1

    def settle(self) -> None:
        """Bring derived spool rows up to date with the records fed so far."""
        self.spool.ensure_session_paths(self.project)

    def finish(self, path: Path) -> SessionInfo | PrivateSessionMarker | None:
        """Materialize the session from the fold (the caller checks I/O errors)."""
        if not self.record_count:
            return None
        if self.private_marker:
            return PrivateSessionMarker(path.stem, "claudecode", self.private_marker)
        spool = self.spool

        (
            session_id,
            thread_id,
            parent_thread_id,
            parent_session_id,
            spawn_depth,
        ) = _claude_session_identity_from_observations(
            path,
            raw_session_id=self.raw_session_id,
            agent_id=self.agent_id,
            sidechain_flag=self.sidechain_flag,
        )
        fallback_day = self.fallback_day
        if fallback_day is None:
            fallback_day = _fallback_usage_day(path, ())
        workspace_root = self.project

        (
            assistant_message_count,
            fresh_input,
            cached_input,
            cache_creation_input,
            cache_creation_5m,
            cache_creation_1h,
            cache_creation_unknown,
            total_output,
        ) = spool.message_totals()
        tool_calls = spool.tool_calls()
        tool_call_count = len(tool_calls)
        self.settle()
        session_paths = spool.session_paths()
        message_usage = spool.message_usage(fallback_day)
        # Every prompt token reaches the model exactly one way: uncached (fresh),
        # written to cache, or read from cache.
        total_input = fresh_input + cache_creation_input + cached_input
        daily = copy.deepcopy(self.daily)
        for v in spool.iter_message_state():
            bucket = _daily_bucket(daily, v[9])
            if bucket is None:
                continue
            bucket.assistant_message_count += 1
            bucket.fresh_input_tokens += v[1]
            bucket.cached_input_tokens += v[2]
            bucket.cache_creation_input_tokens += v[3]
            bucket.cache_creation_5m_input_tokens += v[4]
            bucket.cache_creation_1h_input_tokens += v[5]
            bucket.cache_creation_unknown_input_tokens += v[6]
            bucket.total_input_tokens += v[1] + v[3] + v[2]
            bucket.total_output_tokens += v[7]

        _reconcile_daily_usage(
            daily,
            fallback_day,
            {
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
                "fresh_input_tokens": fresh_input,
                "cached_input_tokens": cached_input,
                "cache_creation_input_tokens": cache_creation_input,
                "cache_creation_5m_input_tokens": cache_creation_5m,
                "cache_creation_1h_input_tokens": cache_creation_1h,
                "cache_creation_unknown_input_tokens": cache_creation_unknown,
                "reasoning_output_tokens": 0,
                "user_message_count": self.user_message_count,
                "assistant_message_count": assistant_message_count,
                "tool_call_count": tool_call_count,
            },
        )

        return SessionInfo(
            session_id=session_id,
            source="claudecode",
            project=self.project,
            first_timestamp=self.first_timestamp,
            last_timestamp=self.last_timestamp,
            user_message_count=self.user_message_count,
            assistant_message_count=assistant_message_count,
            tool_call_count=tool_call_count,
            error_count=self.error_count,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            fresh_input_tokens=fresh_input,
            cached_input_tokens=cached_input,
            cache_creation_input_tokens=cache_creation_input,
            cache_creation_5m_input_tokens=cache_creation_5m,
            cache_creation_1h_input_tokens=cache_creation_1h,
            cache_creation_unknown_input_tokens=cache_creation_unknown,
            first_user_message=self.first_user_message,
            model=self.model,
            workspace_root=workspace_root,
            thread_id=thread_id,
            parent_thread_id=parent_thread_id,
            parent_session_id=parent_session_id,
            spawn_depth=spawn_depth,
            tool_calls=tool_calls,
            session_paths=session_paths,
            daily_usage=_sorted_daily(daily),
            message_usage=message_usage,
        )


def parse_claudecode_session(path: Path) -> SessionInfo | PrivateSessionMarker | None:
    """Parse a Claude Code JSONL file and return a SessionInfo."""
    # Deduplicate assistant messages by message.id, keep last (highest tokens).
    # NOTE: this is per-file only. Resuming a Claude Code session copies the
    # prior history into a NEW file and re-stamps each record's sessionId to
    # the new session, so replayed messages are indistinguishable locally and
    # this file's totals count them again. Cross-session dedup happens in the
    # ledger: each kept message is also emitted as a MessageUsage claim
    # (message_usage) and sync resolves one owning session per claim_key in
    # the message_claims table, which feeds the native_* columns.
    # Cardinality-dependent state lives in a mode-0600 disk spool.  The
    # resulting sequences remain reusable/indexable, but a session with
    # millions of messages or tool calls does not retain millions of Python
    # objects (or an equally large id->index dictionary) in memory.
    stream = _ClaudeStream(_new_spool(path))
    load_stats = JsonlLoadStats()
    for r in _iter_jsonl(path, stats=load_stats):
        stream.feed(r)
    if load_stats.io_errors:
        return None
    return stream.finish(path)


class _CodexStream:
    """Single-pass left fold over one Codex rollout's records.

    This merges what used to be two passes. Everything the second pass took
    from the first is fixed by records at or before the one being folded:
    replay candidacy is decided by the first two valid records, the replay
    boundary is the first matching ``task_started`` at index 2 or later (so no
    later record can reclassify an earlier one), and identity, project, model
    and the fallback day are only read at ``finish``. Timestamps keep both
    passes' rules: ``first_timestamp`` is the first-pass value, or else the
    first truthy record timestamp; ``last_timestamp`` is the last truthy
    record timestamp, or else ``first_timestamp``.
    """

    _SCALARS = (
        "record_count",
        "first_normalized",
        "replay_candidate",
        "replay_boundary",
        "private_marker",
        "project",
        "model",
        "fallback_day",
        "thread_id",
        "meta_first_timestamp",
        "parent_thread_id",
        "parent_session_id",
        "spawn_depth",
        "leaf_meta_found",
        "first_record_timestamp",
        "last_record_timestamp",
        "user_message_count",
        "assistant_message_count",
        "first_user_message",
        "error_count",
        "fresh_input_tokens",
        "cached_input_tokens",
        "total_output_tokens",
        "reasoning_output_tokens",
        "baseline",
    )

    def __init__(self, spool: _ParseSpool, state: dict | None = None) -> None:
        self.spool = spool
        self.record_count = 0
        # (record_type, forked_from_id) of record 0, kept for candidacy.
        self.first_normalized: list | None = None
        self.replay_candidate = False
        self.replay_boundary: int | None = None
        self.private_marker = None
        self.project = "unknown"
        self.model = None
        self.fallback_day = None
        # None means "the path stem", resolved at finish.
        self.thread_id: str | None = None
        self.meta_first_timestamp = None
        self.parent_thread_id = None
        self.parent_session_id = None
        self.spawn_depth = 0
        self.leaf_meta_found = False
        self.first_record_timestamp = None
        self.last_record_timestamp = None
        self.user_message_count = 0
        self.assistant_message_count = 0
        self.first_user_message = ""
        self.error_count = 0
        self.fresh_input_tokens = 0
        self.cached_input_tokens = 0
        self.total_output_tokens = 0
        self.reasoning_output_tokens = 0
        # Codex token_count events carry cumulative counters. A structurally
        # copied prefix establishes the starting counter state but contributes
        # no native usage. Explicit all-zero vectors start a new billing epoch;
        # maxima are retained only inside an epoch so small downward telemetry
        # wobbles clamp to zero without erasing genuine post-reset usage.
        self.baseline = [0, 0, 0, 0]  # current epoch maxima
        self.daily: dict[str, DailyUsage] = {}
        if state is not None:
            for name in self._SCALARS:
                setattr(self, name, state[name])
            self.daily = _daily_from_state(state["daily"])

    def state(self) -> dict:
        data = {name: getattr(self, name) for name in self._SCALARS}
        data["daily"] = _daily_state(self.daily)
        return data

    def feed(self, record: dict) -> None:
        index = self.record_count
        self.record_count += 1
        record_type, payload, timestamp = _normalize_codex_record(record)
        if self.first_normalized is None:
            self.first_normalized = [
                record_type,
                _string_id(payload.get("forked_from_id")),
            ]
            self.thread_id = _string_id(record.get("id"))
            self.meta_first_timestamp = record.get("timestamp")
        elif index == 1:
            first_type, first_forked_from = self.first_normalized
            self.replay_candidate = (
                first_type == "session_meta"
                and record_type == "session_meta"
                and bool(first_forked_from)
                and _string_id(payload.get("id")) == first_forked_from
            )
        elif self.replay_candidate and self.replay_boundary is None:
            if record_type == "event_msg" and payload.get("type") == "task_started":
                started_at = _started_at_epoch_second(payload.get("started_at"))
                record_second = _timestamp_epoch_second(timestamp)
                if started_at is not None and started_at == record_second:
                    self.replay_boundary = index

        if self.private_marker is None:
            self.private_marker = _private_marker((record,))
        if self.fallback_day is None:
            self.fallback_day = _day_of(record.get("timestamp"))
        if self.project == "unknown":
            if record_type in {"session_meta", "turn_context"} and payload.get("cwd"):
                self.project = str(payload["cwd"])
            elif record_type == "message" and payload.get("role") == "user":
                text = _extract_text(payload.get("content", []))
                match = re.search(r"<cwd>(.*?)</cwd>", text, flags=re.DOTALL)
                if match:
                    self.project = match.group(1).strip()

        # A copied ancestor can contain any number of session_meta records.
        # Leaf identity and lineage come exclusively from the first one.
        if record_type == "session_meta" and not self.leaf_meta_found:
            self.leaf_meta_found = True
            self.thread_id = _string_id(payload.get("id")) or self.thread_id
            self.meta_first_timestamp = (
                payload.get("timestamp") or timestamp or self.meta_first_timestamp
            )
            top_level_parent = _string_id(payload.get("parent_thread_id"))
            nested_parent = None
            source = payload.get("source")
            if isinstance(source, dict):
                subagent = source.get("subagent", {})
                if isinstance(subagent, dict):
                    thread_spawn = subagent.get("thread_spawn", {})
                    if isinstance(thread_spawn, dict):
                        nested_parent = _string_id(thread_spawn.get("parent_thread_id"))
                        self.spawn_depth = int(thread_spawn.get("depth", 0) or 0)
            self.parent_thread_id = (
                top_level_parent
                or nested_parent
                or _string_id(payload.get("forked_from_id"))
            )
            self.parent_session_id = self.parent_thread_id
        if record_type == "turn_context" and payload.get("model") and not self.model:
            self.model = payload["model"]

        self._fold_usage(index, record_type, payload, timestamp)

    def _fold_usage(
        self, index: int, record_type: str, payload: dict, timestamp: str | None
    ) -> None:
        if timestamp:
            if not self.first_record_timestamp:
                self.first_record_timestamp = timestamp
            self.last_record_timestamp = timestamp

        in_replay = (
            self.replay_candidate
            and index >= 1
            and (self.replay_boundary is None or index < self.replay_boundary)
        )

        token_totals = _extract_codex_token_totals(record_type, payload)
        if token_totals is not None:
            input_tokens, cached_tokens, output_tokens, reasoning_tokens = token_totals
            current = [input_tokens, cached_tokens, output_tokens, reasoning_tokens]
            baseline = self.baseline
            if any(baseline) and _is_explicit_codex_counter_reset(record_type, payload):
                # Applies while folding replay too: only the terminal
                # inherited epoch may baseline the leaf-native continuation.
                baseline = [0, 0, 0, 0]
            if not in_replay:
                delta_input = max(0, input_tokens - baseline[0])
                delta_cached = max(0, cached_tokens - baseline[1])
                delta_output = max(0, output_tokens - baseline[2])
                delta_reasoning = max(0, reasoning_tokens - baseline[3])
                # Codex input_tokens already includes the cached portion, so
                # fresh is the remainder — adding cached again would
                # double-count it.
                delta_fresh = max(0, delta_input - delta_cached)
                self.fresh_input_tokens += delta_fresh
                self.cached_input_tokens += delta_cached
                self.total_output_tokens += delta_output
                self.reasoning_output_tokens += delta_reasoning
                if delta_fresh or delta_cached or delta_output or delta_reasoning:
                    bucket = _daily_bucket(self.daily, timestamp)
                    if bucket is not None:
                        bucket.fresh_input_tokens += delta_fresh
                        bucket.cached_input_tokens += delta_cached
                        bucket.total_input_tokens += delta_fresh + delta_cached
                        bucket.total_output_tokens += delta_output
                        bucket.reasoning_output_tokens += delta_reasoning
            self.baseline = [max(base, cur) for base, cur in zip(baseline, current)]

        if record_type == "message":
            role = payload.get("role", "")
            content = payload.get("content", [])

            if role == "user":
                text = " ".join(
                    _iter_search_content_text(
                        content,
                        block_types=_CODEX_SEARCH_BLOCK_TYPES,
                        allow_untyped_text=True,
                    )
                )
                clean = _clean_codex_user_text(text)
                if not clean or len(clean) < 3:
                    return
                # Keep first_user_message from the replayed history: it names
                # the session's actual topic. Counts stay live-only. Injected
                # AGENTS.md payloads are not the topic.
                if not self.first_user_message and not _is_codex_agents_payload(clean):
                    self.first_user_message = clean[:500]
                if in_replay:
                    return
                self.user_message_count += 1
                bucket = _daily_bucket(self.daily, timestamp)
                if bucket is not None:
                    bucket.user_message_count += 1

            elif role == "assistant":
                if in_replay:
                    return
                text = _extract_text(content)
                if text.strip():
                    self.assistant_message_count += 1
                    bucket = _daily_bucket(self.daily, timestamp)
                    if bucket is not None:
                        bucket.assistant_message_count += 1

        elif in_replay:
            return

        elif record_type == "function_call":
            tool_name = payload.get("name", "unknown")
            args = _load_tool_args(payload.get("arguments", {}))
            cmd = _extract_command(args)
            self.spool.append_tool_call(
                ToolCall(
                    tool_name=tool_name,
                    command=str(cmd)[:500] if cmd else None,
                    timestamp=timestamp,
                    operation=_infer_operation(tool_name, cmd),
                    input_paths=_extract_argument_paths(args),
                    command_paths=_extract_command_paths(cmd),
                    call_id=payload.get("call_id") or payload.get("id"),
                )
            )
            bucket = _daily_bucket(self.daily, timestamp)
            if bucket is not None:
                bucket.tool_call_count += 1

        elif record_type == "function_call_output":
            _, is_error = _parse_tool_output(payload.get("output", ""))
            if is_error:
                self.error_count += 1
            call_id = payload.get("call_id") or payload.get("id")
            if call_id:
                self.spool.set_tool_result(call_id, is_error)

    def settle(self) -> None:
        """Bring derived spool rows up to date with the records fed so far."""
        self.spool.ensure_session_paths(self.project)

    def finish(self, path: Path) -> SessionInfo | PrivateSessionMarker | None:
        """Materialize the session from the fold (the caller checks I/O errors)."""
        if not self.record_count:
            return None
        if self.private_marker:
            return PrivateSessionMarker(path.stem, "codex", self.private_marker)

        session_id = path.stem
        workspace_root = self.project
        fallback_day = self.fallback_day
        if fallback_day is None:
            fallback_day = _fallback_usage_day(path, ())
        # The first pass's value wins when truthy; otherwise the first truthy
        # record timestamp; otherwise the first pass's (possibly "") value.
        first_timestamp = (
            self.meta_first_timestamp
            or self.first_record_timestamp
            or self.meta_first_timestamp
        )
        last_timestamp = self.last_record_timestamp or self.meta_first_timestamp

        tool_calls = self.spool.tool_calls()
        tool_call_count = len(tool_calls)
        self.settle()
        session_paths = self.spool.session_paths()
        total_input_tokens = self.fresh_input_tokens + self.cached_input_tokens
        daily = copy.deepcopy(self.daily)
        _reconcile_daily_usage(
            daily,
            fallback_day,
            {
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "fresh_input_tokens": self.fresh_input_tokens,
                "cached_input_tokens": self.cached_input_tokens,
                "cache_creation_input_tokens": 0,
                "cache_creation_5m_input_tokens": 0,
                "cache_creation_1h_input_tokens": 0,
                "cache_creation_unknown_input_tokens": 0,
                "reasoning_output_tokens": self.reasoning_output_tokens,
                "user_message_count": self.user_message_count,
                "assistant_message_count": self.assistant_message_count,
                "tool_call_count": tool_call_count,
            },
        )

        return SessionInfo(
            session_id=session_id,
            source="codex",
            project=self.project,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            user_message_count=self.user_message_count,
            assistant_message_count=self.assistant_message_count,
            tool_call_count=tool_call_count,
            error_count=self.error_count,
            total_input_tokens=total_input_tokens,
            total_output_tokens=self.total_output_tokens,
            fresh_input_tokens=self.fresh_input_tokens,
            cached_input_tokens=self.cached_input_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens,
            first_user_message=self.first_user_message,
            model=self.model,
            workspace_root=workspace_root,
            thread_id=self.thread_id or path.stem,
            parent_thread_id=self.parent_thread_id,
            parent_session_id=self.parent_session_id,
            spawn_depth=self.spawn_depth,
            tool_calls=tool_calls,
            session_paths=session_paths,
            daily_usage=_sorted_daily(daily),
        )


def parse_codex_session(path: Path) -> SessionInfo | PrivateSessionMarker | None:
    """Parse a Codex JSONL file and return a SessionInfo."""
    stream = _CodexStream(_new_spool(path))
    load_stats = JsonlLoadStats()
    for record in _iter_jsonl(path, stats=load_stats):
        stream.feed(record)
    if load_stats.io_errors:
        return None
    return stream.finish(path)


# Transcripts below this size spool in memory instead of a temporary file.
IN_MEMORY_SPOOL_MAX_BYTES = 16 * 1024 * 1024
# Bump whenever _ClaudeStream/_CodexStream fold differently or their state
# changes shape: persisted parse states from another version are discarded.
PARSE_STATE_VERSION = 1
# Generation of a checkpoint that records only where a session's first privacy
# marker lies (followed by the marker); such a checkpoint has no state file.
MARKER_GENERATION = "marker:"


def _new_spool(path: Path | TextIO, size: int | None = None) -> _ParseSpool:
    if size is None:
        try:
            size = os.stat(path).st_size
        except (OSError, TypeError):
            size = None
    return _ParseSpool(in_memory=size is not None and size <= IN_MEMORY_SPOOL_MAX_BYTES)


_STREAMS = {"claudecode": _ClaudeStream, "codex": _CodexStream}


@dataclass(frozen=True)
class ParseCheckpoint:
    """How far a persisted parse state has folded one transcript.

    ``offset`` sits just past a line terminator, ``prefix_sha256`` hashes
    bytes [0, offset), and ``generation`` names the exact spool commit, so a
    state file that ran ahead of (or fell behind) the database row is never
    resumed.
    """

    offset: int
    prefix_sha256: str
    generation: str
    dev: int
    ino: int
    version: int = PARSE_STATE_VERSION


@dataclass
class TranscriptParse:
    """A parse result plus the spool its lazy sequences read from."""

    info: SessionInfo | PrivateSessionMarker | None
    # Durable state now covers [0, checkpoint.offset); None if none was kept.
    checkpoint: ParseCheckpoint | None
    # Byte offset parsing started from (0 means a full parse).
    resumed_from: int
    spool: _ParseSpool | None = None

    def close(self) -> None:
        """Release the spool; an uncommitted provisional tail is discarded."""
        if self.spool is not None:
            self.spool.close()
            self.spool = None


def remove_parse_state(state_path: Path) -> None:
    for candidate in (state_path, Path(f"{state_path}-journal")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _resume_stream(source: str, state_path: Path, checkpoint: ParseCheckpoint):
    spool = _ParseSpool(state_path)
    try:
        stored = json.loads(spool.get_meta("stream") or "null")
        if (
            isinstance(stored, dict)
            and stored.get("version") == PARSE_STATE_VERSION == checkpoint.version
            and stored.get("source") == source
            and stored.get("generation") == checkpoint.generation
            and stored.get("offset") == checkpoint.offset
        ):
            return _STREAMS[source](spool, stored["state"])
    except (sqlite3.Error, KeyError, TypeError, ValueError, AttributeError):
        pass
    spool.close()
    return None


def parse_transcript(
    source: str,
    path: Path,
    scan,
    *,
    state_path: Path | None = None,
    checkpoint: ParseCheckpoint | None = None,
) -> TranscriptParse:
    """Parse exactly the ``scan.size`` bytes a TranscriptScan hashed.

    With ``state_path`` the fold is resumable. It resumes from ``checkpoint``
    when the file is the same inode and its first ``checkpoint.offset`` bytes
    hash as recorded; otherwise it restarts from byte 0. Records through the
    last line terminator are committed to the state; an unterminated final
    line is folded provisionally (a full parse includes it when it is valid
    JSON) and rolled back on close, so the next run re-reads it once complete.

    Raises OSError when the file no longer matches the scan (rotated or
    truncated), exactly as a vanished file would.
    """
    from .transcript_io import open_text_range

    stream_type = _STREAMS[source]
    stream = None
    resumed_from = 0
    prefix_unchanged = bool(
        checkpoint is not None
        and checkpoint.version == PARSE_STATE_VERSION
        and (checkpoint.dev, checkpoint.ino) == (scan.dev, scan.ino)
        and checkpoint.offset <= scan.line_end
        and scan.prefix_matches(checkpoint.offset, checkpoint.prefix_sha256)
    )
    if prefix_unchanged and checkpoint.generation.startswith(MARKER_GENERATION):
        # The first privacy marker lies in the unchanged prefix, so a full
        # parse returns exactly this marker whatever was appended.
        return TranscriptParse(
            info=PrivateSessionMarker(
                path.stem, source, checkpoint.generation[len(MARKER_GENERATION) :]
            ),
            checkpoint=checkpoint,
            resumed_from=checkpoint.offset,
        )
    if prefix_unchanged and state_path is not None and state_path.exists():
        try:
            stream = _resume_stream(source, state_path, checkpoint)
        except (OSError, sqlite3.Error):
            stream = None
        if stream is not None:
            resumed_from = checkpoint.offset
    if stream is None:
        if state_path is not None:
            remove_parse_state(state_path)
            spool = _ParseSpool(state_path)
        else:
            spool = _new_spool(path, scan.size)
        stream = stream_type(spool)
    spool = stream.spool
    result = TranscriptParse(
        info=None, checkpoint=None, resumed_from=resumed_from, spool=spool
    )
    try:
        spool.begin()
        with open(path, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (scan.dev, scan.ino) or (
                opened.st_size < scan.size
            ):
                raise FileNotFoundError(
                    errno.ENOENT, "transcript changed after it was hashed", str(path)
                )
            stats = JsonlLoadStats()
            with open_text_range(handle, resumed_from, scan.line_end) as text:
                for record in _iter_jsonl(text, stats=stats):
                    stream.feed(record)
            if stats.io_errors:
                result.close()
                return result
            if spool.persistent and stream.private_marker:
                # A session that opted out of indexing keeps no derived
                # content (first message, commands, paths) between syncs,
                # only where its marker sits: the result is the marker for
                # as long as these bytes are unchanged.
                result.close()
                remove_parse_state(state_path)
                result.checkpoint = ParseCheckpoint(
                    offset=scan.line_end,
                    prefix_sha256=scan.line_end_sha256,
                    generation=MARKER_GENERATION + stream.private_marker,
                    dev=scan.dev,
                    ino=scan.ino,
                )
                result.info = PrivateSessionMarker(
                    path.stem, source, stream.private_marker
                )
                return result
            if spool.persistent:
                stream.settle()
                generation = secrets.token_hex(16)
                spool.set_meta(
                    "stream",
                    json.dumps(
                        {
                            "version": PARSE_STATE_VERSION,
                            "source": source,
                            "generation": generation,
                            "offset": scan.line_end,
                            "state": stream.state(),
                        },
                        ensure_ascii=False,
                    ),
                )
                spool.commit()
                result.checkpoint = ParseCheckpoint(
                    offset=scan.line_end,
                    prefix_sha256=scan.line_end_sha256,
                    generation=generation,
                    dev=scan.dev,
                    ino=scan.ino,
                )
                spool.begin()
            if scan.size > scan.line_end:
                tail_stats = JsonlLoadStats()
                with open_text_range(handle, scan.line_end, scan.size) as text:
                    for record in _iter_jsonl(text, stats=tail_stats):
                        stream.feed(record)
                if tail_stats.io_errors:
                    result.close()
                    return result
        result.info = stream.finish(path)
        if isinstance(result.info, PrivateSessionMarker) and state_path is not None:
            # The marker is only in the unterminated tail: keep nothing.
            result.close()
            remove_parse_state(state_path)
            result.checkpoint = None
    except BaseException:
        result.close()
        raise
    return result


def find_private_marker(path: Path) -> str | None:
    """The first privacy marker in a transcript, exactly as a parse finds it.

    Both streams check every valid record with _private_marker and keep the
    first hit, so this returns the marker a full parse of ``path`` would.
    """
    for record in _iter_jsonl(path, report_malformed=False):
        marker = _private_marker((record,))
        if marker:
            return marker
    return None


def file_hash(path: Path) -> str:
    """SHA256 of the full file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def render_claudecode_transcript(path: Path | TextIO) -> list:
    """Return list of turn dicts for transcript display."""
    turns = []
    seen_msg_ids = set()
    load_stats = JsonlLoadStats()

    for r in _iter_jsonl(path, stats=load_stats):
        rtype = r.get("type", "")
        ts = r.get("timestamp")

        if rtype == "user":
            msg = r.get("message", {})
            content = msg.get("content", "")

            if isinstance(content, list):
                # Check for tool results
                tool_results = [
                    b
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                if tool_results:
                    for block in tool_results:
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            result_content = "\n".join(
                                b.get("text", "")
                                for b in result_content
                                if isinstance(b, dict)
                            )
                        turns.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.get("tool_use_id", ""),
                                "content": str(result_content)[:5000],
                                "is_error": block.get("is_error", False),
                                "timestamp": ts,
                            }
                        )
                    continue

            text = _extract_text(content)
            if not text.strip():
                continue
            turns.append(
                {
                    "type": "user",
                    "content": text,
                    "timestamp": ts,
                    "cwd": r.get("cwd"),
                }
            )

        elif rtype == "assistant":
            msg = r.get("message", {})
            msg_id = msg.get("id")
            if msg_id:
                if msg_id in seen_msg_ids:
                    continue
                seen_msg_ids.add(msg_id)

            content = msg.get("content", [])
            if not isinstance(content, list):
                continue

            usage = msg.get("usage", {})
            blocks = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    blocks.append({"type": "text", "text": block.get("text", "")})
                elif btype == "thinking":
                    blocks.append(
                        {"type": "thinking", "text": block.get("thinking", "")}
                    )
                elif btype == "tool_use":
                    blocks.append(
                        {
                            "type": "tool_use",
                            "name": block.get("name", ""),
                            "id": block.get("id", ""),
                            "input": block.get("input", {}),
                        }
                    )

            if not blocks:
                continue

            turns.append(
                {
                    "type": "assistant",
                    "blocks": blocks,
                    "timestamp": ts,
                    "model": msg.get("model"),
                    "input_tokens": usage.get("input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                }
            )

    return [] if load_stats.io_errors else turns


def render_codex_transcript(path: Path | TextIO) -> list:
    """Return list of turn dicts for Codex transcript display."""
    turns = []
    load_stats = JsonlLoadStats()

    for record in _iter_jsonl(path, stats=load_stats):
        record_type, payload, timestamp = _normalize_codex_record(record)

        if record_type == "message":
            role = payload.get("role", "")
            content = payload.get("content", [])

            if role == "user":
                text = _extract_text(content)
                clean = _clean_codex_user_text(text)
                if not clean or len(clean) < 3:
                    continue
                turns.append({"type": "user", "content": clean, "timestamp": timestamp})

            elif role == "assistant":
                blocks = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") in {
                        "text",
                        "output_text",
                        "input_text",
                    } and block.get("text"):
                        blocks.append({"type": "text", "text": block["text"]})

                if not blocks:
                    text = _extract_text(content)
                    if text.strip():
                        blocks = [{"type": "text", "text": text}]

                if blocks:
                    turns.append(
                        {
                            "type": "assistant",
                            "blocks": blocks,
                            "timestamp": timestamp,
                            "model": None,
                            "input_tokens": 0,
                            "output_tokens": 0,
                        }
                    )

        elif record_type == "function_call":
            tool_name = payload.get("name", "unknown")
            args = _load_tool_args(payload.get("arguments", {}))
            cmd = _extract_command(args)
            turns.append(
                {
                    "type": "tool_use",
                    "name": tool_name,
                    "id": payload.get("call_id", ""),
                    "input": args or ({"command": cmd} if cmd else {}),
                    "timestamp": timestamp,
                }
            )

        elif record_type == "function_call_output":
            display, is_error = _parse_tool_output(payload.get("output", ""))
            turns.append(
                {
                    "type": "tool_result",
                    "tool_use_id": payload.get("call_id", ""),
                    "content": str(display)[:5000],
                    "is_error": is_error,
                    "timestamp": timestamp,
                }
            )

        elif record_type == "reasoning":
            summaries = payload.get("summary", [])
            text = " ".join(s.get("text", "") for s in summaries if isinstance(s, dict))
            if text.strip():
                turns.append({"type": "thinking", "text": text, "timestamp": timestamp})

    return [] if load_stats.io_errors else turns
