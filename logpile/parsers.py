"""Parse Claude Code and Codex JSONL session files."""
import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


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
    date distorts any date-bucketed rollup. Records without timestamps are
    excluded, so daily sums can undercount session totals slightly.
    """

    day: str  # YYYY-MM-DD (UTC)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    fresh_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    user_message_count: int = 0
    assistant_message_count: int = 0
    tool_call_count: int = 0


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
    reasoning_output_tokens: int = 0
    first_user_message: str = ""
    model: Optional[str] = None
    workspace_root: Optional[str] = None
    parent_session_id: Optional[str] = None
    spawn_depth: int = 0
    tool_calls: list = field(default_factory=list)
    session_paths: list = field(default_factory=list)
    daily_usage: list = field(default_factory=list)


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


def _load_jsonl(path: Path) -> list:
    records = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
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
    totals = info.get("total_token_usage", {})
    if not isinstance(totals, dict):
        return None
    input_tokens = int(totals.get("input_tokens", 0) or 0)
    cached_input_tokens = int(totals.get("cached_input_tokens", 0) or 0)
    output_tokens = int(totals.get("output_tokens", 0) or 0)
    reasoning_output_tokens = int(totals.get("reasoning_output_tokens", 0) or 0)
    return input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens


def _day_of(timestamp: Optional[str]) -> Optional[str]:
    if not timestamp or len(timestamp) < 10:
        return None
    return timestamp[:10]


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


def _codex_replay_window(records: list) -> tuple[int, int]:
    """Detect the replay burst a Codex resume/fork snapshot starts with.

    Resuming a session writes a NEW rollout file containing a full copy of
    the prior history, re-stamped into a single wall-clock second (and given
    a fresh session id), followed by the live continuation. Counting the
    replayed records would count the same history once per snapshot.

    Live turns never produce two token_count events inside one second, so:
    the file is a snapshot iff its first two token_count events share a
    wall-clock second S. The replayed history is then the contiguous run of
    records stamped S (records without timestamps ride along with the run).

    Returns (start, end) record indices of the replay window, or (0, 0) if
    the file is not a snapshot.
    """
    first_second = None
    token_events_in_second = 0
    for record in records:
        record_type, payload, timestamp = _normalize_codex_record(record)
        if _extract_codex_token_totals(record_type, payload) is None:
            continue
        second = (timestamp or "")[:19]
        if first_second is None:
            if not second:
                return 0, 0
            first_second = second
            token_events_in_second = 1
            continue
        if second == first_second:
            token_events_in_second += 1
        break
    if token_events_in_second < 2:
        return 0, 0

    start = end = None
    for index, record in enumerate(records):
        _, _, timestamp = _normalize_codex_record(record)
        second = (timestamp or "")[:19]
        if second == first_second:
            if start is None:
                start = index
            end = index + 1
        elif start is not None:
            if timestamp:
                break
            end = index + 1
    if start is None:
        return 0, 0
    return start, end


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


def _is_private(records: list) -> bool:
    """Check if a session contains a privacy marker."""
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

        if any(marker in text for marker in _PRIVATE_MARKERS for text in texts if text):
            return True
    return False


def parse_claudecode_session(path: Path) -> Optional[SessionInfo]:
    """Parse a Claude Code JSONL file and return a SessionInfo."""
    records = _load_jsonl(path)
    if not records:
        return None
    if _is_private(records):
        return None

    session_id = path.stem

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
    # aggregate rollups count them once per resume file. Deduplicating those
    # requires cross-session state (usage-tracker does it globally by
    # message.id + requestId).
    seen_msg: dict = {}  # message_id -> {"fresh_input": ..., "cached_input": ..., "output": ..., ...}
    first_timestamp = None
    last_timestamp = None
    user_message_count = 0
    tool_calls = []
    tool_call_index_by_id: dict[str, int] = {}
    first_user_message = ""
    error_count = 0
    model = None
    parent_session_id = None
    spawn_depth = 0
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
            breakdown = usage.get("cache_creation")
            cw5 = cw1 = 0
            if isinstance(breakdown, dict):
                cw5 = int(breakdown.get("ephemeral_5m_input_tokens", 0) or 0)
                cw1 = int(breakdown.get("ephemeral_1h_input_tokens", 0) or 0)
            if cw5 + cw1 == 0:
                cw5 = cache_creation  # no breakdown -> assume 5m
            out = usage.get("output_tokens", 0)
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
                        "output": out,
                        "model": mdl,
                        "timestamp": ts,
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
        bucket.total_input_tokens += v["fresh_input"] + v["cache_creation"] + v["cached_input"]
        bucket.total_output_tokens += v["output"]

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
        first_user_message=first_user_message,
        model=model,
        workspace_root=workspace_root,
        parent_session_id=parent_session_id,
        spawn_depth=spawn_depth,
        tool_calls=tool_calls,
        session_paths=_build_session_paths(tool_calls, workspace_root),
        daily_usage=_sorted_daily(daily),
    )


def parse_codex_session(path: Path) -> Optional[SessionInfo]:
    """Parse a Codex JSONL file and return a SessionInfo."""
    records = _load_jsonl(path)
    if not records:
        return None
    if _is_private(records):
        return None

    first_rec = records[0]
    session_id = first_rec.get("id") or path.stem
    first_timestamp = first_rec.get("timestamp")
    project = _extract_codex_project(records)
    workspace_root = project
    model = None
    parent_session_id = None
    spawn_depth = 0

    for record in records:
        record_type, payload, timestamp = _normalize_codex_record(record)
        if record_type == "session_meta":
            session_id = payload.get("id") or session_id
            first_timestamp = payload.get("timestamp") or timestamp or first_timestamp
            source = payload.get("source")
            if isinstance(source, dict):
                subagent = source.get("subagent", {})
                if isinstance(subagent, dict):
                    thread_spawn = subagent.get("thread_spawn", {})
                    if isinstance(thread_spawn, dict):
                        parent_session_id = thread_spawn.get("parent_thread_id")
                        spawn_depth = int(thread_spawn.get("depth", 0) or 0)
        elif record_type == "turn_context" and payload.get("model") and not model:
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

    # Codex token_count events carry CUMULATIVE counters, and resume/fork
    # snapshot files replay the whole prior history (see
    # _codex_replay_window). Taking max() of the counter would re-count that
    # history once per snapshot, so instead: fold the replay window into a
    # delta baseline and accumulate clamped per-event deltas — each file
    # contributes only its live continuation. Deltas are clamped at zero per
    # component so a counter reset can't go negative or double-count.
    replay_start, replay_end = _codex_replay_window(records)
    baseline = [0, 0, 0, 0]  # input, cached_input, output, reasoning (cumulative maxima)

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
                if delta_fresh or delta_cached or delta_output:
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
        total_input_tokens=fresh_input_tokens + cached_input_tokens,
        total_output_tokens=total_output_tokens,
        fresh_input_tokens=fresh_input_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        first_user_message=first_user_message,
        model=model,
        workspace_root=workspace_root,
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
