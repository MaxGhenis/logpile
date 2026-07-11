"""Parse Claude Code and Codex JSONL session files."""
import hashlib
import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    tool_name: str
    command: Optional[str] = None
    timestamp: Optional[str] = None
    is_error: bool = False
    operation: str = "other"
    input_paths: list[str] = field(default_factory=list)
    command_paths: list[str] = field(default_factory=list)
    call_id: Optional[str] = None


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
    day: Optional[str]  # YYYY-MM-DD (UTC), None when the record has no timestamp
    model: Optional[str]
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
    relative_path: Optional[str]
    operation: str
    source: str
    repo_relative_path: Optional[str] = None
    tool_name: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class SessionInfo:
    session_id: str
    source: str  # 'claudecode' or 'codex'
    project: str
    first_timestamp: Optional[str] = None
    last_timestamp: Optional[str] = None
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
    model: Optional[str] = None
    workspace_root: Optional[str] = None
    thread_id: Optional[str] = None
    parent_thread_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    spawn_depth: int = 0
    tool_calls: list = field(default_factory=list)
    session_paths: list = field(default_factory=list)
    daily_usage: list = field(default_factory=list)
    message_usage: list = field(default_factory=list)  # MessageUsage, claudecode only


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
    if "id" in record and record_id is not None and not isinstance(
        record_id, (str, int)
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


def _normalize_codex_record(record: dict) -> tuple[str, dict, Optional[str]]:
    """Return (record_type, payload, timestamp) for old and new Codex formats."""
    record_type = record.get("type", record.get("record_type", ""))
    timestamp = record.get("timestamp")

    if record_type == "response_item" and isinstance(record.get("payload"), dict):
        payload = record["payload"]
        return payload.get("type", ""), payload, timestamp

    if record_type in {"session_meta", "event_msg", "turn_context"} and isinstance(record.get("payload"), dict):
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


def _extract_codex_token_totals(record_type: str, payload: dict) -> tuple[int, int, int, int] | None:
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


def _day_of(timestamp: Optional[str]) -> Optional[str]:
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
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date().isoformat()


def _daily_bucket(daily: dict, timestamp: Optional[str]) -> Optional[DailyUsage]:
    day = _day_of(timestamp)
    if day is None:
        return None
    bucket = daily.get(day)
    if bucket is None:
        bucket = daily[day] = DailyUsage(day=day)
    return bucket


def _sorted_daily(daily: dict) -> list[DailyUsage]:
    return [daily[day] for day in sorted(daily)]


def _string_id(value: Any) -> Optional[str]:
    if isinstance(value, (str, int)) and str(value):
        return str(value)
    return None


def _timestamp_epoch_second(timestamp: Optional[str]) -> Optional[int]:
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
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _started_at_epoch_second(value: Any) -> Optional[int]:
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


def _extract_command(arguments: dict) -> Optional[str]:
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
    if any(part in lowered for part in ("edit", "write", "replace", "patch", "apply", "create", "delete", "rename", "move")):
        return "write"
    if any(part in lowered for part in ("read", "open", "view", "load", "cat")):
        return "read"
    if any(part in lowered for part in ("search", "find", "grep", "glob", "list", "ls", "scan")):
        return "search"
    if any(part in lowered for part in ("bash", "shell", "exec", "terminal", "command")):
        try:
            tokens = shlex.split(command or "")
        except ValueError:
            tokens = (command or "").split()
        cmd_name = Path(tokens[0]).name if tokens else ""
        if cmd_name in {"rg", "grep", "fd", "find", "ls", "tree", "git"}:
            return "search"
        if cmd_name in {"cat", "bat", "sed", "head", "tail"}:
            return "read"
        if cmd_name in {"mv", "cp", "rm", "touch", "mkdir", "tee", "apply_patch", "prettier", "black", "isort", "gofmt", "rustfmt", "clang-format", "stylua"}:
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
        or lowered.endswith("_path")
        or lowered.endswith("_file")
        or lowered.endswith("_files")
    )


def _clean_path_candidate(value: str) -> str:
    candidate = str(value).strip().strip("\"'")
    candidate = candidate.rstrip(",;")
    if candidate.startswith("file://"):
        candidate = candidate[7:]
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


def _normalize_session_path(candidate: str, workspace_root: str | None) -> tuple[str, str | None, str] | None:
    raw_path = _clean_path_candidate(candidate)
    if not _looks_like_path(raw_path):
        return None

    root = _safe_expanduser(Path(workspace_root)) if workspace_root and workspace_root != "unknown" else None
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


def _build_session_paths(tool_calls: list[ToolCall], workspace_root: str | None) -> list[SessionPath]:
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
            content = msg.get("content") or record.get("content") or payload.get("content") or ""
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
    return datetime.fromtimestamp(modified, tz=timezone.utc).date().isoformat()


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
            int(getattr(bucket, field_name, 0) or 0)
            for bucket in daily.values()
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
) -> tuple[str, str, Optional[str], Optional[str], int]:
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
            raw_agent_id if raw_agent_id.startswith("agent-") else f"agent-{raw_agent_id}"
        )
        parent_thread_id = raw_session_id or root_from_path
        depth = max(1, len(subagent_indices))
        return canonical_id, raw_agent_id, parent_thread_id, parent_thread_id, depth

    thread_id = raw_session_id or path.stem
    return path.stem, thread_id, None, None, 0


def parse_claudecode_session(path: Path) -> Optional[SessionInfo | PrivateSessionMarker]:
    """Parse a Claude Code JSONL file and return a SessionInfo."""
    records = _load_jsonl(path)
    if not records:
        return None
    private_marker = _private_marker(records)
    if private_marker:
        return PrivateSessionMarker(path.stem, "claudecode", private_marker)

    (
        session_id,
        thread_id,
        parent_thread_id,
        parent_session_id,
        spawn_depth,
    ) = _claude_session_identity(path, records)
    fallback_day = _fallback_usage_day(path, records)

    # Get project from cwd field
    project = "unknown"
    for r in records:
        if r.get("cwd"):
            project = r["cwd"]
            break
    workspace_root = project

    # Deduplicate assistant messages by message.id, keep last (highest tokens).
    # NOTE: this is per-file only. Resuming a Claude Code session copies the
    # prior history into a NEW file and re-stamps each record's sessionId to
    # the new session, so replayed messages are indistinguishable locally and
    # this file's totals count them again. Cross-session dedup happens in the
    # ledger: each kept message is also emitted as a MessageUsage claim
    # (message_usage) and sync resolves one owning session per claim_key in
    # the message_claims table, which feeds the native_* columns.
    seen_msg: dict = {}  # message_id -> {"fresh_input": ..., "cached_input": ..., "output": ..., ...}
    first_timestamp = None
    last_timestamp = None
    user_message_count = 0
    tool_calls = []
    tool_call_index_by_id: dict[str, int] = {}
    first_user_message = ""
    error_count = 0
    model = None
    daily: dict[str, DailyUsage] = {}

    for r in records:
        rtype = r.get("type", "")
        ts = r.get("timestamp")
        if ts:
            if not first_timestamp:
                first_timestamp = ts
            last_timestamp = ts

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
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            is_error = bool(block.get("is_error"))
                            if is_error:
                                error_count += 1
                            tool_use_id = block.get("tool_use_id")
                            if tool_use_id and tool_use_id in tool_call_index_by_id:
                                tool_calls[tool_call_index_by_id[tool_use_id]].is_error = is_error
                    continue

            text = _extract_text(content)
            if text.strip():
                user_message_count += 1
                if not first_user_message:
                    first_user_message = text.strip()[:500]
                bucket = _daily_bucket(daily, ts)
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
                prev = seen_msg.get(msg_id)
                if prev is None or out > prev["output"]:
                    seen_msg[msg_id] = {
                        "fresh_input": fresh_inp,
                        "cached_input": cached_inp,
                        "cache_creation": cache_creation,
                        "cache_creation_5m": cw5,
                        "cache_creation_1h": cw1,
                        "cache_creation_unknown": cw_unknown,
                        "output": out,
                        "model": mdl,
                        "timestamp": ts,
                        "request_id": r.get("requestId"),
                        "uuid": r.get("uuid"),
                    }
                if mdl and not model:
                    model = mdl

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
                        command_paths = _extract_command_paths(str(cmd) if cmd else None)
                        tool_calls.append(ToolCall(
                            tool_name=tool_name,
                            command=str(cmd)[:500] if cmd else None,
                            timestamp=ts,
                            operation=_infer_operation(tool_name, str(cmd) if cmd else None),
                            input_paths=input_paths,
                            command_paths=command_paths,
                            call_id=tool_use_id,
                        ))
                        if tool_use_id:
                            tool_call_index_by_id[tool_use_id] = len(tool_calls) - 1
                        bucket = _daily_bucket(daily, ts)
                        if bucket is not None:
                            bucket.tool_call_count += 1

    assistant_message_count = len(seen_msg)
    fresh_input = sum(v["fresh_input"] for v in seen_msg.values())
    cached_input = sum(v["cached_input"] for v in seen_msg.values())
    cache_creation_input = sum(v["cache_creation"] for v in seen_msg.values())
    cache_creation_5m = sum(v["cache_creation_5m"] for v in seen_msg.values())
    cache_creation_1h = sum(v["cache_creation_1h"] for v in seen_msg.values())
    cache_creation_unknown = sum(
        v["cache_creation_unknown"] for v in seen_msg.values()
    )
    # Every prompt token reaches the model exactly one way: uncached (fresh),
    # written to cache, or read from cache.
    total_input = fresh_input + cache_creation_input + cached_input
    total_output = sum(v["output"] for v in seen_msg.values())

    for v in seen_msg.values():
        bucket = _daily_bucket(daily, v["timestamp"])
        if bucket is None:
            continue
        bucket.assistant_message_count += 1
        bucket.fresh_input_tokens += v["fresh_input"]
        bucket.cached_input_tokens += v["cached_input"]
        bucket.cache_creation_input_tokens += v["cache_creation"]
        bucket.cache_creation_5m_input_tokens += v["cache_creation_5m"]
        bucket.cache_creation_1h_input_tokens += v["cache_creation_1h"]
        bucket.cache_creation_unknown_input_tokens += v["cache_creation_unknown"]
        bucket.total_input_tokens += v["fresh_input"] + v["cache_creation"] + v["cached_input"]
        bucket.total_output_tokens += v["output"]

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
            "user_message_count": user_message_count,
            "assistant_message_count": assistant_message_count,
            "tool_call_count": len(tool_calls),
        },
    )

    message_usage = []
    for msg_id, v in seen_msg.items():
        if v["request_id"]:
            claim_key = f"{msg_id}:{v['request_id']}"
        elif v["uuid"]:
            claim_key = f"uuid:{v['uuid']}"
        else:
            claim_key = f"mid:{msg_id}"
        message_usage.append(MessageUsage(
            claim_key=claim_key,
            day=_day_of(v["timestamp"]) or fallback_day,
            model=v["model"],
            fresh_input_tokens=v["fresh_input"],
            cached_input_tokens=v["cached_input"],
            cache_creation_input_tokens=v["cache_creation"],
            cache_creation_5m_input_tokens=v["cache_creation_5m"],
            cache_creation_1h_input_tokens=v["cache_creation_1h"],
            cache_creation_unknown_input_tokens=v["cache_creation_unknown"],
            output_tokens=v["output"],
        ))

    return SessionInfo(
        session_id=session_id,
        source="claudecode",
        project=project,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        user_message_count=user_message_count,
        assistant_message_count=assistant_message_count,
        tool_call_count=len(tool_calls),
        error_count=error_count,
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        fresh_input_tokens=fresh_input,
        cached_input_tokens=cached_input,
        cache_creation_input_tokens=cache_creation_input,
        cache_creation_5m_input_tokens=cache_creation_5m,
        cache_creation_1h_input_tokens=cache_creation_1h,
        cache_creation_unknown_input_tokens=cache_creation_unknown,
        first_user_message=first_user_message,
        model=model,
        workspace_root=workspace_root,
        thread_id=thread_id,
        parent_thread_id=parent_thread_id,
        parent_session_id=parent_session_id,
        spawn_depth=spawn_depth,
        tool_calls=tool_calls,
        session_paths=_build_session_paths(tool_calls, workspace_root),
        daily_usage=_sorted_daily(daily),
        message_usage=message_usage,
    )


def parse_codex_session(path: Path) -> Optional[SessionInfo | PrivateSessionMarker]:
    """Parse a Codex JSONL file and return a SessionInfo."""
    records = _load_jsonl(path)
    if not records:
        return None
    private_marker = _private_marker(records)
    if private_marker:
        return PrivateSessionMarker(path.stem, "codex", private_marker)

    first_rec = records[0]
    session_id = path.stem
    thread_id = _string_id(first_rec.get("id")) or path.stem
    first_timestamp = first_rec.get("timestamp")
    project = _extract_codex_project(records)
    workspace_root = project
    model = None
    parent_thread_id = None
    parent_session_id = None
    spawn_depth = 0
    fallback_day = _fallback_usage_day(path, records)

    # A copied ancestor can contain any number of session_meta records.  Leaf
    # identity and lineage come exclusively from the first one in this file.
    leaf_meta_found = False
    for record in records:
        record_type, payload, timestamp = _normalize_codex_record(record)
        if record_type == "session_meta" and not leaf_meta_found:
            leaf_meta_found = True
            thread_id = _string_id(payload.get("id")) or thread_id
            first_timestamp = payload.get("timestamp") or timestamp or first_timestamp
            top_level_parent = _string_id(payload.get("parent_thread_id"))
            nested_parent = None
            source = payload.get("source")
            if isinstance(source, dict):
                subagent = source.get("subagent", {})
                if isinstance(subagent, dict):
                    thread_spawn = subagent.get("thread_spawn", {})
                    if isinstance(thread_spawn, dict):
                        nested_parent = _string_id(
                            thread_spawn.get("parent_thread_id")
                        )
                        spawn_depth = int(thread_spawn.get("depth", 0) or 0)
            parent_thread_id = (
                top_level_parent
                or nested_parent
                or _string_id(payload.get("forked_from_id"))
            )
            # Sync resolves this raw thread reference to the parent's
            # canonical rollout filename stem before persistence.
            parent_session_id = parent_thread_id
        if record_type == "turn_context" and payload.get("model") and not model:
            model = payload["model"]

    user_message_count = 0
    assistant_message_count = 0
    tool_calls = []
    tool_call_index_by_id: dict[str, int] = {}
    first_user_message = ""
    error_count = 0
    last_timestamp = first_timestamp
    fresh_input_tokens = 0
    cached_input_tokens = 0
    total_output_tokens = 0
    reasoning_output_tokens = 0
    daily: dict[str, DailyUsage] = {}

    # Codex token_count events carry cumulative counters. A structurally
    # copied prefix establishes the starting counter state but contributes no
    # native usage. Explicit all-zero vectors start a new billing epoch;
    # maxima are retained only inside an epoch so small downward telemetry
    # wobbles clamp to zero without erasing genuine post-reset usage.
    replay_start, replay_end = _codex_replay_window(records)
    baseline = [0, 0, 0, 0]  # current epoch maxima

    for index, record in enumerate(records):
        record_type, payload, timestamp = _normalize_codex_record(record)
        if timestamp:
            if not first_timestamp:
                first_timestamp = timestamp
            last_timestamp = timestamp

        in_replay = replay_start <= index < replay_end

        token_totals = _extract_codex_token_totals(record_type, payload)
        if token_totals is not None:
            input_tokens, cached_tokens, output_tokens, reasoning_tokens = token_totals
            current = [input_tokens, cached_tokens, output_tokens, reasoning_tokens]
            if any(baseline) and _is_explicit_codex_counter_reset(
                record_type, payload
            ):
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
                fresh_input_tokens += delta_fresh
                cached_input_tokens += delta_cached
                total_output_tokens += delta_output
                reasoning_output_tokens += delta_reasoning
                if delta_fresh or delta_cached or delta_output or delta_reasoning:
                    bucket = _daily_bucket(daily, timestamp)
                    if bucket is not None:
                        bucket.fresh_input_tokens += delta_fresh
                        bucket.cached_input_tokens += delta_cached
                        bucket.total_input_tokens += delta_fresh + delta_cached
                        bucket.total_output_tokens += delta_output
                        bucket.reasoning_output_tokens += delta_reasoning
            baseline = [max(base, cur) for base, cur in zip(baseline, current)]

        if record_type == "message":
            role = payload.get("role", "")
            content = payload.get("content", [])

            if role == "user":
                text = _extract_text(content)
                clean = _clean_codex_user_text(text)
                if not clean or len(clean) < 3:
                    continue
                # Keep first_user_message from the replayed history: it names
                # the session's actual topic. Counts stay live-only.
                if not first_user_message:
                    first_user_message = clean[:500]
                if in_replay:
                    continue
                user_message_count += 1
                bucket = _daily_bucket(daily, timestamp)
                if bucket is not None:
                    bucket.user_message_count += 1

            elif role == "assistant":
                if in_replay:
                    continue
                text = _extract_text(content)
                if text.strip():
                    assistant_message_count += 1
                    bucket = _daily_bucket(daily, timestamp)
                    if bucket is not None:
                        bucket.assistant_message_count += 1

        elif in_replay:
            continue

        elif record_type == "function_call":
            tool_name = payload.get("name", "unknown")
            args = _load_tool_args(payload.get("arguments", {}))
            cmd = _extract_command(args)
            tool_calls.append(ToolCall(
                tool_name=tool_name,
                command=str(cmd)[:500] if cmd else None,
                timestamp=timestamp,
                operation=_infer_operation(tool_name, cmd),
                input_paths=_extract_argument_paths(args),
                command_paths=_extract_command_paths(cmd),
                call_id=payload.get("call_id") or payload.get("id"),
            ))
            call_id = payload.get("call_id") or payload.get("id")
            if call_id:
                tool_call_index_by_id[str(call_id)] = len(tool_calls) - 1
            bucket = _daily_bucket(daily, timestamp)
            if bucket is not None:
                bucket.tool_call_count += 1

        elif record_type == "function_call_output":
            _, is_error = _parse_tool_output(payload.get("output", ""))
            if is_error:
                error_count += 1
            call_id = payload.get("call_id") or payload.get("id")
            if call_id and str(call_id) in tool_call_index_by_id:
                tool_calls[tool_call_index_by_id[str(call_id)]].is_error = is_error

    total_input_tokens = fresh_input_tokens + cached_input_tokens
    _reconcile_daily_usage(
        daily,
        fallback_day,
        {
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "fresh_input_tokens": fresh_input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "cache_creation_input_tokens": 0,
            "cache_creation_5m_input_tokens": 0,
            "cache_creation_1h_input_tokens": 0,
            "cache_creation_unknown_input_tokens": 0,
            "reasoning_output_tokens": reasoning_output_tokens,
            "user_message_count": user_message_count,
            "assistant_message_count": assistant_message_count,
            "tool_call_count": len(tool_calls),
        },
    )

    return SessionInfo(
        session_id=session_id,
        source="codex",
        project=project,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        user_message_count=user_message_count,
        assistant_message_count=assistant_message_count,
        tool_call_count=len(tool_calls),
        error_count=error_count,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        fresh_input_tokens=fresh_input_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        first_user_message=first_user_message,
        model=model,
        workspace_root=workspace_root,
        thread_id=thread_id,
        parent_thread_id=parent_thread_id,
        parent_session_id=parent_session_id,
        spawn_depth=spawn_depth,
        tool_calls=tool_calls,
        session_paths=_build_session_paths(tool_calls, workspace_root),
        daily_usage=_sorted_daily(daily),
    )


def file_hash(path: Path) -> str:
    """SHA256 of the full file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def render_claudecode_transcript(path: Path) -> list:
    """Return list of turn dicts for transcript display."""
    records = _load_jsonl(path)
    turns = []
    seen_msg_ids = set()

    for r in records:
        rtype = r.get("type", "")
        ts = r.get("timestamp")

        if rtype == "user":
            msg = r.get("message", {})
            content = msg.get("content", "")

            if isinstance(content, list):
                # Check for tool results
                tool_results = [
                    b for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                if tool_results:
                    for block in tool_results:
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            result_content = "\n".join(
                                b.get("text", "") for b in result_content
                                if isinstance(b, dict)
                            )
                        turns.append({
                            "type": "tool_result",
                            "tool_use_id": block.get("tool_use_id", ""),
                            "content": str(result_content)[:5000],
                            "is_error": block.get("is_error", False),
                            "timestamp": ts,
                        })
                    continue

            text = _extract_text(content)
            if not text.strip():
                continue
            turns.append({
                "type": "user",
                "content": text,
                "timestamp": ts,
                "cwd": r.get("cwd"),
            })

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
                    blocks.append({"type": "thinking", "text": block.get("thinking", "")})
                elif btype == "tool_use":
                    blocks.append({
                        "type": "tool_use",
                        "name": block.get("name", ""),
                        "id": block.get("id", ""),
                        "input": block.get("input", {}),
                    })

            if not blocks:
                continue

            turns.append({
                "type": "assistant",
                "blocks": blocks,
                "timestamp": ts,
                "model": msg.get("model"),
                "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            })

    return turns


def render_codex_transcript(path: Path) -> list:
    """Return list of turn dicts for Codex transcript display."""
    records = _load_jsonl(path)
    turns = []

    for record in records:
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
                    if block.get("type") in {"text", "output_text", "input_text"} and block.get("text"):
                        blocks.append({"type": "text", "text": block["text"]})

                if not blocks:
                    text = _extract_text(content)
                    if text.strip():
                        blocks = [{"type": "text", "text": text}]

                if blocks:
                    turns.append({
                        "type": "assistant",
                        "blocks": blocks,
                        "timestamp": timestamp,
                        "model": None,
                        "input_tokens": 0,
                        "output_tokens": 0,
                    })

        elif record_type == "function_call":
            tool_name = payload.get("name", "unknown")
            args = _load_tool_args(payload.get("arguments", {}))
            cmd = _extract_command(args)
            turns.append({
                "type": "tool_use",
                "name": tool_name,
                "id": payload.get("call_id", ""),
                "input": args or ({"command": cmd} if cmd else {}),
                "timestamp": timestamp,
            })

        elif record_type == "function_call_output":
            display, is_error = _parse_tool_output(payload.get("output", ""))
            turns.append({
                "type": "tool_result",
                "tool_use_id": payload.get("call_id", ""),
                "content": str(display)[:5000],
                "is_error": is_error,
                "timestamp": timestamp,
            })

        elif record_type == "reasoning":
            summaries = payload.get("summary", [])
            text = " ".join(s.get("text", "") for s in summaries if isinstance(s, dict))
            if text.strip():
                turns.append({"type": "thinking", "text": text, "timestamp": timestamp})

    return turns
