"""Consume Subfleet's credential-free integration event spool.

Only the dedicated ``integration-events/v1`` directory is admissible. This
module does not know Subfleet's private run-ledger layout and deliberately
stores normalized allowlisted columns rather than raw event JSON.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import stat
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SUBFLEET_EVENT_SCHEMA_VERSION = 1
MAX_SPOOL_FILE_BYTES = 256 * 1024
_EVENT_TYPES = {"run.started", "run.bound", "run.finished"}
_PROVIDERS = {"claude", "codex", "unknown"}
_REF_RE = re.compile(r"^(?P<kind>task|attempt|run|event|lane|workspace)_[0-9a-f]{24}$")
_RUN_FILE_RE = re.compile(r"^(run_[0-9a-f]{24})\.jsonl$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_EVENT_COLUMNS = (
    "event_id",
    "schema_version",
    "event_type",
    "task_id",
    "run_id",
    "attempt_id",
    "provider",
    "lane_ref",
    "workspace_ref",
    "occurred_at",
    "started_at",
    "finished_at",
    "binding_kind",
    "binding_native_id",
    "outcome_status",
    "outcome_exit_code",
    "outcome_duration_s",
)


class SubfleetSpoolError(RuntimeError):
    """The configured integration seam is not the safe v1 spool."""


@dataclass(frozen=True)
class SubfleetEvent:
    event_id: str
    schema_version: int
    event_type: str
    task_id: str
    run_id: str
    attempt_id: str | None
    provider: str
    lane_ref: str | None
    workspace_ref: str | None
    occurred_at: str
    started_at: str | None
    finished_at: str | None
    binding_kind: str | None
    binding_native_id: str | None
    outcome_status: str | None
    outcome_exit_code: int | None
    outcome_duration_s: float | None

    def values(self) -> tuple[Any, ...]:
        return astuple(self)


@dataclass(frozen=True)
class SubfleetIngestResult:
    inserted: int = 0
    duplicates: int = 0
    rejected: int = 0
    reconciled: int = 0


def default_subfleet_spool(home: Path) -> Path:
    """Return Subfleet's dedicated v1 spool without inspecting private state."""

    override = os.environ.get("SUBFLEET_STATE_DIR")
    state_dir = (
        Path(override).expanduser()
        if override
        else Path(home) / "chief-of-staff" / "state" / "subfleet"
    )
    return state_dir / "integration-events" / "v1"


def _require_exact_keys(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    keys = set(value)
    if keys != required | (keys & optional):
        missing = sorted(required - keys)
        unexpected = sorted(keys - required - optional)
        raise ValueError(
            f"event keys do not match v1 allowlist; missing={missing}, "
            f"unexpected={unexpected}"
        )


def _ref(value: Any, kind: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{kind} reference must be a string")
    match = _REF_RE.fullmatch(value)
    if match is None or match.group("kind") != kind:
        raise ValueError(f"invalid {kind} reference")
    return value


def _timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError(f"{name} must be a bounded ISO-8601 timestamp")
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"invalid {name}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    try:
        return parsed.astimezone(UTC).isoformat()
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"invalid {name}") from exc


def _parse_timestamps(
    value: Any,
    *,
    required: set[str],
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError("timestamps must be an object")
    _require_exact_keys(value, required=required)
    return {name: _timestamp(value[name], f"timestamps.{name}") for name in required}


def _parse_event(value: Any, *, expected_run_id: str) -> SubfleetEvent:
    if not isinstance(value, dict):
        raise TypeError("event must be an object")
    event_type = value.get("event")
    if event_type not in _EVENT_TYPES:
        raise ValueError("unsupported integration event type")

    common = {
        "schema_version",
        "event",
        "event_id",
        "task_id",
        "run_id",
        "provider",
        "lane_ref",
        "workspace",
        "timestamps",
    }
    if event_type == "run.started":
        _require_exact_keys(value, required=common)
    elif event_type == "run.bound":
        _require_exact_keys(value, required=common | {"attempt_id", "binding"})
    else:
        _require_exact_keys(
            value,
            required=common | {"outcome"},
            optional={"attempt_id"},
        )

    schema_version = value["schema_version"]
    if (
        isinstance(schema_version, bool)
        or schema_version != SUBFLEET_EVENT_SCHEMA_VERSION
    ):
        raise ValueError("unsupported integration event schema")
    event_id = _ref(value["event_id"], "event")
    task_id = _ref(value["task_id"], "task")
    run_id = _ref(value["run_id"], "run")
    if run_id != expected_run_id:
        raise ValueError("event run_id does not match its spool filename")
    provider = value["provider"]
    if provider not in _PROVIDERS:
        raise ValueError("unsupported provider")
    lane_ref = _ref(value["lane_ref"], "lane", optional=True)

    workspace = value["workspace"]
    if not isinstance(workspace, dict):
        raise TypeError("workspace must be an object")
    _require_exact_keys(workspace, required={"ref"})
    workspace_ref = _ref(workspace["ref"], "workspace", optional=True)

    attempt_id = None
    binding_kind = None
    binding_native_id = None
    outcome_status = None
    outcome_exit_code = None
    outcome_duration_s = None
    started_at = None
    finished_at = None

    if event_type == "run.started":
        timestamps = _parse_timestamps(
            value["timestamps"], required={"occurred_at", "started_at"}
        )
        started_at = timestamps["started_at"]
    elif event_type == "run.bound":
        if provider not in {"claude", "codex"}:
            raise ValueError("run.bound requires a native provider")
        attempt_id = _ref(value["attempt_id"], "attempt")
        binding = value["binding"]
        if not isinstance(binding, dict):
            raise ValueError("binding must be an object")
        _require_exact_keys(binding, required={"kind", "native_id"})
        binding_kind = binding["kind"]
        expected_kind = "session" if provider == "claude" else "thread"
        if binding_kind != expected_kind:
            raise ValueError("binding kind does not match provider")
        native_id = binding["native_id"]
        if not isinstance(native_id, str) or _UUID_RE.fullmatch(native_id) is None:
            raise ValueError("binding.native_id must be a UUID")
        binding_native_id = native_id.lower()
        timestamps = _parse_timestamps(value["timestamps"], required={"occurred_at"})
    else:
        if "attempt_id" in value:
            attempt_id = _ref(value["attempt_id"], "attempt")
            if provider not in {"claude", "codex"}:
                raise ValueError("an attempted run requires a native provider")
        timestamps = _parse_timestamps(
            value["timestamps"],
            required={"occurred_at", "started_at", "finished_at"},
        )
        started_at = timestamps["started_at"]
        finished_at = timestamps["finished_at"]
        outcome = value["outcome"]
        if not isinstance(outcome, dict):
            raise ValueError("outcome must be an object")
        _require_exact_keys(
            outcome,
            required={"status", "exit_code"},
            optional={"duration_s"},
        )
        outcome_status = outcome["status"]
        outcome_exit_code = outcome["exit_code"]
        if outcome_status not in {"succeeded", "failed"}:
            raise ValueError("invalid outcome status")
        if isinstance(outcome_exit_code, bool) or not isinstance(
            outcome_exit_code, int
        ):
            raise ValueError("outcome.exit_code must be an integer")
        if not -(2**63) <= outcome_exit_code < 2**63:
            raise ValueError("outcome.exit_code is outside SQLite integer range")
        expected_status = "succeeded" if outcome_exit_code == 0 else "failed"
        if outcome_status != expected_status:
            raise ValueError("outcome status conflicts with exit code")
        if "duration_s" in outcome:
            duration = outcome["duration_s"]
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                raise ValueError("outcome.duration_s must be finite and nonnegative")
            try:
                normalized_duration = float(duration)
            except OverflowError as exc:
                raise ValueError(
                    "outcome.duration_s must be finite and nonnegative"
                ) from exc
            if not math.isfinite(normalized_duration) or normalized_duration < 0:
                raise ValueError("outcome.duration_s must be finite and nonnegative")
            outcome_duration_s = normalized_duration

    return SubfleetEvent(
        event_id=event_id,
        schema_version=schema_version,
        event_type=event_type,
        task_id=task_id,
        run_id=run_id,
        attempt_id=attempt_id,
        provider=provider,
        lane_ref=lane_ref,
        workspace_ref=workspace_ref,
        occurred_at=timestamps["occurred_at"],
        started_at=started_at,
        finished_at=finished_at,
        binding_kind=binding_kind,
        binding_native_id=binding_native_id,
        outcome_status=outcome_status,
        outcome_exit_code=outcome_exit_code,
        outcome_duration_s=outcome_duration_s,
    )


def _open_spool(spool_dir: Path) -> int | None:
    if spool_dir.name != "v1" or spool_dir.parent.name != "integration-events":
        raise SubfleetSpoolError(
            "Subfleet event source must be the dedicated integration-events/v1 spool"
        )
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise SubfleetSpoolError(
            "This platform cannot safely pin the Subfleet event spool"
        )
    flags = os.O_RDONLY | no_follow | directory | getattr(os, "O_CLOEXEC", 0)
    try:
        parent_mode = spool_dir.parent.lstat().st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SubfleetSpoolError(
            f"Could not inspect Subfleet event spool: {exc}"
        ) from exc
    if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
        raise SubfleetSpoolError(
            "Refusing non-directory or symlinked Subfleet event spool component"
        )
    try:
        parent_fd = os.open(spool_dir.parent, flags)
    except OSError as exc:
        raise SubfleetSpoolError(
            f"Could not safely open Subfleet event spool parent: {exc}"
        ) from exc
    try:
        try:
            spool_mode = os.stat(
                spool_dir.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            ).st_mode
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SubfleetSpoolError(
                f"Could not inspect Subfleet event spool: {exc}"
            ) from exc
        if stat.S_ISLNK(spool_mode) or not stat.S_ISDIR(spool_mode):
            raise SubfleetSpoolError(
                "Refusing non-directory or symlinked Subfleet event spool component"
            )
        try:
            return os.open(spool_dir.name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise SubfleetSpoolError(
                f"Could not safely open Subfleet event spool: {exc}"
            ) from exc
    finally:
        os.close(parent_fd)


def _read_spool_file(spool_fd: int, name: str) -> bytes | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=spool_fd)
    except OSError:
        return None
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
            or stat.S_IMODE(file_stat.st_mode) & 0o077
        ):
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(MAX_SPOOL_FILE_BYTES + 1)
        if len(payload) > MAX_SPOOL_FILE_BYTES:
            return None
        return payload
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _event_conflicts(conn: sqlite3.Connection, event: SubfleetEvent) -> bool | None:
    existing = conn.execute(
        f"SELECT {', '.join(_EVENT_COLUMNS)} FROM subfleet_events WHERE event_id = ?",
        (event.event_id,),
    ).fetchone()
    if existing is None:
        return None
    return tuple(existing[column] for column in _EVENT_COLUMNS) != event.values()


def _attempt_conflicts(conn: sqlite3.Connection, event: SubfleetEvent) -> bool:
    if event.event_type != "run.bound":
        return False
    existing = conn.execute(
        """
        SELECT attempt_id, provider, binding_kind, native_id
        FROM subfleet_attempts
        WHERE attempt_id = ? OR (provider = ? AND native_id = ?)
        """,
        (event.attempt_id, event.provider, event.binding_native_id),
    ).fetchall()
    expected = (
        event.attempt_id,
        event.provider,
        event.binding_kind,
        event.binding_native_id,
    )
    return any(tuple(row) != expected for row in existing)


def _lifecycle_conflicts(conn: sqlite3.Connection, event: SubfleetEvent) -> bool:
    existing_run = conn.execute(
        """
        SELECT task_id, provider
        FROM subfleet_events
        WHERE run_id = ?
        ORDER BY event_id
        LIMIT 1
        """,
        (event.run_id,),
    ).fetchone()
    if existing_run is not None and (
        existing_run["task_id"] != event.task_id
        or existing_run["provider"] != event.provider
    ):
        return True
    if event.event_type in {"run.started", "run.finished"}:
        duplicate_lifecycle = conn.execute(
            """
            SELECT event_id
            FROM subfleet_events
            WHERE run_id = ? AND event_type = ?
            LIMIT 1
            """,
            (event.run_id, event.event_type),
        ).fetchone()
        if (
            duplicate_lifecycle is not None
            and duplicate_lifecycle["event_id"] != event.event_id
        ):
            return True
    elif event.event_type == "run.bound":
        duplicate_binding = conn.execute(
            """
            SELECT event_id
            FROM subfleet_events
            WHERE run_id = ? AND event_type = 'run.bound' AND attempt_id = ?
            LIMIT 1
            """,
            (event.run_id, event.attempt_id),
        ).fetchone()
        if (
            duplicate_binding is not None
            and duplicate_binding["event_id"] != event.event_id
        ):
            return True
    if event.attempt_id is None:
        return False
    existing_attempt = conn.execute(
        """
        SELECT provider
        FROM subfleet_attempts
        WHERE attempt_id = ?
        """,
        (event.attempt_id,),
    ).fetchone()
    if existing_attempt is not None and existing_attempt["provider"] != event.provider:
        return True
    existing_event = conn.execute(
        """
        SELECT 1
        FROM subfleet_events
        WHERE attempt_id = ? AND provider != ?
        LIMIT 1
        """,
        (event.attempt_id, event.provider),
    ).fetchone()
    return existing_event is not None


def _store_event(
    conn: sqlite3.Connection,
    event: SubfleetEvent,
    *,
    ingested_at: str,
) -> str:
    conflict = _event_conflicts(conn, event)
    if (
        conflict is True
        or _lifecycle_conflicts(conn, event)
        or _attempt_conflicts(conn, event)
    ):
        return "rejected"
    if event.event_type == "run.bound":
        conn.execute(
            """
            INSERT INTO subfleet_attempts (
                attempt_id, provider, binding_kind, native_id, first_bound_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(attempt_id) DO UPDATE SET
                first_bound_at = MIN(first_bound_at, excluded.first_bound_at)
            """,
            (
                event.attempt_id,
                event.provider,
                event.binding_kind,
                event.binding_native_id,
                event.occurred_at,
            ),
        )
    if conflict is False:
        return "duplicate"
    placeholders = ", ".join("?" for _ in (*_EVENT_COLUMNS, "ingested_at"))
    conn.execute(
        f"""
        INSERT INTO subfleet_events ({", ".join((*_EVENT_COLUMNS, "ingested_at"))})
        VALUES ({placeholders})
        """,
        (*event.values(), ingested_at),
    )
    return "inserted"


def _session_for_attempt(
    conn: sqlite3.Connection,
    *,
    provider: str,
    native_id: str,
) -> str | None:
    if provider == "claude":
        source = "claudecode"
        preferred = "session_id"
    else:
        source = "codex"
        preferred = "thread_id"
    row = conn.execute(
        f"""
        SELECT session_id
        FROM sessions
        WHERE source = ?
          AND (
              {preferred} = ? COLLATE NOCASE
              OR session_id = ? COLLATE NOCASE
          )
        ORDER BY
            CASE WHEN {preferred} = ? COLLATE NOCASE THEN 0 ELSE 1 END,
            COALESCE(first_timestamp, '~'),
            session_id
        LIMIT 1
        """,
        (source, native_id, native_id, native_id),
    ).fetchone()
    return str(row["session_id"]) if row is not None else None


def reconcile_subfleet_attempts(
    conn: sqlite3.Connection,
    *,
    reconciled_at: str | None = None,
) -> int:
    """Join stored native bindings to current Logpile session rows."""

    reconciled_at = reconciled_at or datetime.now(UTC).isoformat()
    changed = 0
    attempts = conn.execute(
        """
        SELECT attempt_id, provider, native_id, session_id
        FROM subfleet_attempts
        ORDER BY attempt_id
        """
    ).fetchall()
    for attempt in attempts:
        session_id = _session_for_attempt(
            conn,
            provider=attempt["provider"],
            native_id=attempt["native_id"],
        )
        if session_id == attempt["session_id"]:
            continue
        conn.execute(
            """
            UPDATE subfleet_attempts
            SET session_id = ?, reconciled_at = ?
            WHERE attempt_id = ?
            """,
            (session_id, reconciled_at, attempt["attempt_id"]),
        )
        changed += 1
    return changed


def ingest_subfleet_events(
    conn: sqlite3.Connection,
    spool_dir: Path,
    *,
    ingested_at: str | None = None,
) -> SubfleetIngestResult:
    """Ingest the v1 spool idempotently and reconcile all known attempts."""

    spool_dir = Path(spool_dir)
    ingested_at = ingested_at or datetime.now(UTC).isoformat()
    inserted = duplicates = rejected = 0
    spool_fd = _open_spool(spool_dir)
    if spool_fd is not None:
        try:
            try:
                entries = sorted(os.listdir(spool_fd))
            except (OSError, TypeError) as exc:
                raise SubfleetSpoolError(
                    f"Could not safely enumerate Subfleet event spool: {exc}"
                ) from exc
            for name in entries:
                match = _RUN_FILE_RE.fullmatch(name)
                if match is None:
                    if name.endswith(".jsonl"):
                        rejected += 1
                    continue
                payload = _read_spool_file(spool_fd, name)
                if payload is None:
                    rejected += 1
                    continue
                try:
                    lines = payload.decode("utf-8").splitlines()
                except UnicodeError:
                    rejected += 1
                    continue
                for line in lines:
                    try:
                        event = _parse_event(
                            json.loads(line),
                            expected_run_id=match.group(1),
                        )
                    except (RecursionError, TypeError, ValueError):
                        rejected += 1
                        continue
                    result = _store_event(conn, event, ingested_at=ingested_at)
                    if result == "inserted":
                        inserted += 1
                    elif result == "duplicate":
                        duplicates += 1
                    else:
                        rejected += 1
        finally:
            os.close(spool_fd)
    reconciled = reconcile_subfleet_attempts(conn, reconciled_at=ingested_at)
    return SubfleetIngestResult(
        inserted=inserted,
        duplicates=duplicates,
        rejected=rejected,
        reconciled=reconciled,
    )


def get_task_timeline(
    conn: sqlite3.Connection,
    task_id: str,
) -> list[dict[str, Any]]:
    """Return one deterministic, session-joined Subfleet task timeline."""

    if _ref(task_id, "task") is None:
        raise ValueError("invalid task reference")
    rows = conn.execute(
        """
        SELECT *
        FROM subfleet_task_timeline
        WHERE task_id = ?
        ORDER BY
            occurred_at,
            CASE event_type
                WHEN 'run.started' THEN 0
                WHEN 'run.bound' THEN 1
                WHEN 'run.finished' THEN 2
                ELSE 3
            END,
            event_id
        """,
        (task_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_tasks(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent local Subfleet tasks with compact lifecycle summaries."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("task limit must be an integer from 1 to 1000")
    rows = conn.execute(
        """
        SELECT *
        FROM subfleet_task_catalog
        ORDER BY last_occurred_at DESC, task_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    result = []
    for row in rows:
        task = dict(row)
        task["providers"] = [
            provider
            for provider, column in (
                ("claude", "has_claude"),
                ("codex", "has_codex"),
                ("unknown", "has_unknown"),
            )
            if task.pop(column)
        ]
        result.append(task)
    return result
