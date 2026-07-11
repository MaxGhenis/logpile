"""Sync JSONL sessions to a shared directory and update the SQLite index."""
import errno
import fcntl
import fnmatch
import os
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .parsers import parse_claudecode_session, parse_codex_session, file_hash
from .objectives import SESSION_OBJECTIVE_VERSION, derive_session_objective
from .origins import SESSION_ORIGIN_VERSION, derive_session_origin
from .db import (
    ensure_user,
    get_user_by_identifier,
    get_db,
    init_db,
    insert_session_daily_usage,
    insert_session_paths,
    insert_tool_calls,
    normalize_username,
    resolve_session_visibility,
    upsert_session,
)


SESSION_ACTIVITY_VERSION = 1
SESSION_NARRATIVE_VERSION = 1
# 4: codex replay-burst deltas (resume snapshots no longer re-count history),
#    Claude cache_creation capture, per-day usage rows, file size/mtime.
SESSION_TOKEN_VERSION = 4
_TEST_PATTERNS = (
    re.compile(r"\b(pytest|py\.test|vitest|jest|nosetests|rspec)\b"),
    re.compile(r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+(pytest|unittest)\b"),
    re.compile(r"\b(go|deno|mix)\s+test\b"),
    re.compile(r"\bcargo\s+test\b"),
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b"),
    re.compile(r"\buv\s+run\s+pytest\b"),
)
_LINT_PATTERNS = (
    re.compile(r"\b(?:ruff(?:\s+check)?|flake8|pylint|eslint|shellcheck|hadolint|mypy|pyright)\b"),
    re.compile(r"\bbiome\s+check\b"),
    re.compile(r"\bgolangci-lint\b"),
    re.compile(r"\btsc\b.*--noemit\b"),
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?lint\b"),
)
_BUILD_PATTERNS = (
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build\b"),
    re.compile(r"\b(?:next|vite|webpack|rollup|parcel)\s+build\b"),
    re.compile(r"\bcargo\s+build\b"),
    re.compile(r"\bgo\s+build\b"),
    re.compile(r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+build\b"),
    re.compile(r"\btsc\b(?!.*--noemit\b)"),
)
_FORMAT_PATTERNS = (
    re.compile(r"\b(?:prettier|black|isort|gofmt|rustfmt|clang-format|stylua)\b"),
    re.compile(r"\bruff\s+format\b"),
    re.compile(r"\bbiome\s+format\b"),
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?format\b"),
)


def _safe_expanduser(path: Path) -> Path:
    try:
        return path.expanduser()
    except RuntimeError:
        return path


def _codex_session_roots(home: Path) -> list[Path]:
    """All Codex rollout roots under a home dir.

    Live sessions come first so that when a rollout exists both live and
    archived (mid-archive race), the live copy wins the per-run stem dedup.
    ~/.codex/archived_sessions alone held 26GB that sync never saw.
    """
    roots = [
        home / ".codex" / "sessions",
        home / ".codex" / "archived_sessions",
        home / ".codex-2" / "sessions",
        home / ".codex-3" / "sessions",
    ]
    openclaw_agents = home / ".openclaw" / "agents"
    if openclaw_agents.exists():
        roots.extend(sorted(openclaw_agents.glob("*/agent/codex-home/sessions")))
    return [root for root in roots if root.exists()]


def _unchanged_on_disk(existing_row, jsonl_path: Path) -> bool:
    """Cheap no-op check for a synced session: same path, same size+mtime,
    and the shared copy (when one is expected) still present.

    file_hash() reads the whole file, and _copy_session hashes both sides
    again — tens of GB per sync once immutable archives are scanned. Any
    mismatch here just falls through to the full hash-and-parse path.
    """
    if existing_row["file_size"] is None or existing_row["file_mtime"] is None:
        return False
    if existing_row["source_path"] != str(jsonl_path):
        return False
    try:
        stat = jsonl_path.stat()
    except OSError:
        return False
    if existing_row["file_size"] != stat.st_size:
        return False
    if abs(existing_row["file_mtime"] - stat.st_mtime) > 1e-6:
        return False
    shared_path = existing_row["shared_path"] or ""
    if existing_row["visibility"] == "private":
        return shared_path == ""
    if not shared_path:
        return False
    shared = Path(shared_path)
    return shared.exists() or shared.is_symlink()


def _resolve_sync_username(conn, requested_username: str) -> str:
    direct = get_user_by_identifier(conn, requested_username)
    if direct:
        return direct["username"]

    normalized = normalize_username(requested_username)
    direct = get_user_by_identifier(conn, normalized)
    if direct:
        return direct["username"]

    rows = conn.execute("SELECT username FROM users ORDER BY updated_at DESC, username").fetchall()
    if len(rows) == 1:
        return rows[0]["username"]
    return normalized


def _copy_session(src: Path, dst: Path) -> bool:
    """Copy a session file into the shared directory.

    Atomic: writes to a temp file beside dst, then os.replace()s it into
    place, so the shared copy is always a complete file (old or new) even if
    sync dies mid-copy. A symlink dst (from an earlier ENOSPC fallback) is
    upgraded to a real copy once a copy succeeds, and an existing complete
    copy is never removed in favor of a failed one.
    """
    was_symlink = dst.is_symlink()
    if not was_symlink and dst.exists() and file_hash(dst) == file_hash(src):
        return False
    # Per-writer tmp name: reconcile_session_storage (publish/visibility paths)
    # copies without holding the sync lock, so a shared tmp name would let one
    # writer os.replace() another's half-written file into place.
    tmp = dst.with_name(f"{dst.name}.{os.getpid()}.tmp-sync")
    try:
        shutil.copy2(src, tmp)
        tmp.replace(dst)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        if exc.errno != errno.ENOSPC:
            raise
        if was_symlink:
            try:
                if dst.resolve() == src.resolve():
                    return False
            except OSError:
                pass
        elif dst.exists():
            return False
        if dst.is_symlink():
            dst.unlink()
        dst.symlink_to(src.resolve())
    return True


def _remove_shared_copy(path: Path | None) -> bool:
    if path is None:
        return False
    if not path.exists() and not path.is_symlink():
        return False
    path.unlink()
    return True


def _desired_shared_path(
    shared_dir: Path,
    username: str,
    source: str,
    project: str,
    filename: str,
) -> Path:
    return shared_dir / username / source / project / filename


def _sync_shared_copy(
    *,
    src: Path,
    shared_dir: Path,
    username: str,
    source: str,
    project: str,
    filename: str,
    visibility: str,
    existing_shared_path: str | None = None,
) -> tuple[str, bool]:
    desired_path = _desired_shared_path(shared_dir, username, source, project, filename)
    changed = False

    stale_candidates: list[Path] = []
    if existing_shared_path:
        stale_candidates.append(Path(existing_shared_path))
    stale_candidates.append(desired_path)

    seen: set[str] = set()
    unique_candidates: list[Path] = []
    for candidate in stale_candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(candidate)

    if visibility == "private":
        for candidate in unique_candidates:
            changed = _remove_shared_copy(candidate) or changed
        return "", changed

    desired_path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in unique_candidates:
        if candidate == desired_path:
            continue
        changed = _remove_shared_copy(candidate) or changed
    changed = _copy_session(src, desired_path) or changed
    return str(desired_path), changed


def _effective_storage_visibility(
    existing_row,
    resolved_visibility: dict[str, str | int | None],
) -> str:
    if existing_row and (existing_row["visibility_source"] or "default") == "manual":
        return existing_row["visibility"] or resolved_visibility["visibility"]
    return resolved_visibility["visibility"]


def reconcile_session_storage(
    conn,
    *,
    shared_dir: Path,
    session_id: str | None = None,
    session_id_prefix: str | None = None,
    username: str | None = None,
) -> int:
    clauses: list[str] = []
    params: list[str] = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if session_id_prefix:
        clauses.append("session_id LIKE ?")
        params.append(f"{session_id_prefix}%")
    if username:
        clauses.append("username = ?")
        params.append(username)
    where = " AND ".join(clauses) if clauses else "1 = 1"

    rows = conn.execute(
        f"""
        SELECT
            session_id,
            username,
            source,
            project,
            source_path,
            shared_path,
            visibility
        FROM sessions
        WHERE {where}
        """,
        params,
    ).fetchall()

    updated = 0
    for row in rows:
        src = Path(row["source_path"])
        current_shared_path = row["shared_path"] or ""
        next_shared_path = current_shared_path
        storage_changed = False

        if row["visibility"] == "private":
            next_shared_path, storage_changed = _sync_shared_copy(
                src=src,
                shared_dir=shared_dir,
                username=row["username"],
                source=row["source"],
                project=row["project"] or "unknown",
                filename=src.name,
                visibility="private",
                existing_shared_path=current_shared_path or None,
            )
        elif src.exists():
            next_shared_path, storage_changed = _sync_shared_copy(
                src=src,
                shared_dir=shared_dir,
                username=row["username"],
                source=row["source"],
                project=row["project"] or "unknown",
                filename=src.name,
                visibility=row["visibility"],
                existing_shared_path=current_shared_path or None,
            )

        if storage_changed or next_shared_path != current_shared_path:
            conn.execute(
                "UPDATE sessions SET shared_path = ? WHERE session_id = ?",
                (next_shared_path, row["session_id"]),
            )
            updated += 1

    return updated


def load_ignore_patterns(home: Path) -> list[str]:
    patterns = []
    for name in (".logpile-ignore", ".agentus-ignore"):
        ignore_file = home / name
        if not ignore_file.exists():
            continue
        with open(ignore_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns


def should_ignore(path: Path, patterns: list[str]) -> bool:
    name = str(path)
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(path.name, pattern):
            return True
    return False


def project_from_claude_path(jsonl_path: Path) -> str:
    """
    Claude encodes project paths as directory names like:
      -Users-maxghenis-PolicyEngine-policyengine-us
    We decode and return the leaf directory name.
    """
    dir_name = jsonl_path.parent.name  # e.g. -Users-maxghenis-PolicyEngine-policyengine-us
    if dir_name.startswith("-"):
        dir_name = dir_name[1:]
    # Claude replaces '/' with '-', but so does '-' in repo names, so just take last segment
    parts = dir_name.split("-")
    # Return the last non-empty, non-UUID-looking part
    for part in reversed(parts):
        if part and len(part) > 1:
            return part
    return dir_name or "unknown"


def _git_output(workspace: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    output = result.stdout.strip()
    return output or None


def _resolve_repo_metadata(
    workspace_root: str | None,
    cache: dict[str, dict[str, str | int | None]],
) -> dict[str, str | int | None]:
    key = (workspace_root or "").strip()
    if key in cache:
        return cache[key]

    if not key or key == "unknown":
        cache[key] = {
            "worktree_root": None,
            "repo_root": None,
            "repo_name": None,
            "git_branch": None,
            "git_commit": None,
            "git_dirty": 0,
        }
        return cache[key]

    workspace = _safe_expanduser(Path(key))
    if workspace.exists():
        try:
            workspace = workspace.resolve()
        except OSError:
            pass
    fallback_worktree_root = str(workspace)
    fallback_repo_name = workspace.name or None
    metadata: dict[str, str | int | None] = {
        "worktree_root": fallback_worktree_root,
        "repo_root": None,
        "repo_name": fallback_repo_name,
        "git_branch": None,
        "git_commit": None,
        "git_dirty": 0,
    }
    if not workspace.exists():
        cache[key] = metadata
        return metadata

    worktree_root = _git_output(
        workspace,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
    )
    if not worktree_root:
        cache[key] = metadata
        return metadata

    common_dir = _git_output(
        workspace,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    repo_root = worktree_root
    if common_dir:
        common_path = Path(common_dir)
        if common_path.name == ".git":
            repo_root = str(common_path.parent)

    git_branch = _git_output(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if git_branch == "HEAD":
        git_branch = None

    metadata = {
        "worktree_root": worktree_root,
        "repo_root": repo_root,
        "repo_name": Path(repo_root).name or Path(worktree_root).name or fallback_repo_name,
        "git_branch": git_branch,
        "git_commit": _git_output(workspace, "rev-parse", "HEAD"),
        "git_dirty": 1 if _git_output(workspace, "status", "--porcelain") else 0,
    }
    cache[key] = metadata
    return metadata


def _normalize_workspace_root(workspace_root: str | None) -> str | None:
    value = (workspace_root or "").strip()
    if not value or value == "unknown":
        return None
    path = _safe_expanduser(Path(value))
    if path.exists():
        try:
            path = path.resolve()
        except OSError:
            pass
    return str(path)


def _repo_relative_path(normalized_path: str | None, root: str | None) -> str | None:
    if not normalized_path or not root:
        return None
    # A path captured from tool-call args can contain a null byte (or other
    # garbage); such a string can't be a real repo-relative path, and
    # Path.resolve() raises ValueError on it. Skip it rather than crash sync.
    if "\x00" in normalized_path or "\x00" in root:
        return None
    path = _safe_expanduser(Path(normalized_path))
    root_path = _safe_expanduser(Path(root))
    try:
        path = path.resolve(strict=False)
    except (OSError, ValueError):
        pass
    try:
        root_path = root_path.resolve(strict=False)
    except (OSError, ValueError):
        pass
    try:
        relative = str(path.relative_to(root_path))
    except ValueError:
        return None
    return relative if relative not in {"", "."} else None


def _annotate_session_paths(
    session_paths: list,
    *,
    repo_root: str | None,
    worktree_root: str | None,
    workspace_root: str | None,
) -> list:
    canonical_root = worktree_root or workspace_root
    annotated = []
    for session_path in session_paths:
        annotated.append(
            replace(
                session_path,
                repo_relative_path=_repo_relative_path(
                    session_path.normalized_path,
                    canonical_root,
                ),
            )
        )
    return annotated


def _matches_any(command: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(command) for pattern in patterns)


def _derive_session_activity(tool_calls: list, session_paths: list) -> dict[str, int]:
    metrics = {
        "write_path_count": 0,
        "read_path_count": 0,
        "search_path_count": 0,
        "test_run_count": 0,
        "test_failure_count": 0,
        "lint_run_count": 0,
        "lint_failure_count": 0,
        "build_run_count": 0,
        "build_failure_count": 0,
        "format_run_count": 0,
        "format_failure_count": 0,
        "git_status_count": 0,
        "git_diff_count": 0,
        "git_commit_count": 0,
        "activity_version": SESSION_ACTIVITY_VERSION,
    }

    unique_paths_by_operation = {
        "write": set(),
        "read": set(),
        "search": set(),
    }
    for session_path in session_paths:
        operation = session_path.operation
        if operation in unique_paths_by_operation:
            unique_paths_by_operation[operation].add(session_path.normalized_path)

    metrics["write_path_count"] = len(unique_paths_by_operation["write"])
    metrics["read_path_count"] = len(unique_paths_by_operation["read"])
    metrics["search_path_count"] = len(unique_paths_by_operation["search"])

    for tool_call in tool_calls:
        command = (tool_call.command or "").strip().lower()
        if not command:
            continue

        is_test = _matches_any(command, _TEST_PATTERNS)
        is_lint = _matches_any(command, _LINT_PATTERNS)
        is_build = _matches_any(command, _BUILD_PATTERNS)
        is_format = _matches_any(command, _FORMAT_PATTERNS)

        if is_test:
            metrics["test_run_count"] += 1
            if tool_call.is_error:
                metrics["test_failure_count"] += 1
        if is_lint:
            metrics["lint_run_count"] += 1
            if tool_call.is_error:
                metrics["lint_failure_count"] += 1
        if is_build:
            metrics["build_run_count"] += 1
            if tool_call.is_error:
                metrics["build_failure_count"] += 1
        if is_format:
            metrics["format_run_count"] += 1
            if tool_call.is_error:
                metrics["format_failure_count"] += 1
        if re.search(r"\bgit\s+status\b", command):
            metrics["git_status_count"] += 1
        if re.search(r"\bgit\s+diff\b", command):
            metrics["git_diff_count"] += 1
        if re.search(r"\bgit\s+commit\b", command):
            metrics["git_commit_count"] += 1

    return metrics


def _clip_text(value: str | None, limit: int = 160) -> str | None:
    if not value:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _sample_paths(session_paths: list, limit: int = 2) -> list[str]:
    samples: list[str] = []
    seen: set[str] = set()
    for session_path in session_paths:
        if session_path.operation != "write":
            continue
        label = session_path.repo_relative_path or session_path.relative_path or session_path.display_path
        if not label or label in seen:
            continue
        seen.add(label)
        samples.append(label)
        if len(samples) >= limit:
            break
    return samples


def _status_from_activity(activity: dict[str, int], error_count: int) -> str:
    made_changes = (
        (activity.get("write_path_count") or 0) > 0
        or (activity.get("format_run_count") or 0) > 0
        or (activity.get("git_commit_count") or 0) > 0
    )
    passed_checks = any(
        (activity.get(total_key) or 0) > (activity.get(failure_key) or 0)
        for total_key, failure_key in (
            ("test_run_count", "test_failure_count"),
            ("build_run_count", "build_failure_count"),
            ("lint_run_count", "lint_failure_count"),
        )
    )
    has_failures = (
        error_count > 0
        or (activity.get("test_failure_count") or 0) > 0
        or (activity.get("build_failure_count") or 0) > 0
        or (activity.get("lint_failure_count") or 0) > 0
        or (activity.get("format_failure_count") or 0) > 0
    )
    if has_failures:
        if made_changes or passed_checks:
            return "partial"
        return "failed"
    if made_changes or passed_checks:
        return "success"
    return "exploration"


def _derive_session_narrative(
    *,
    source: str,
    project: str,
    repo_name: str | None,
    first_user_message: str | None,
    error_count: int,
    activity: dict[str, int],
    session_paths: list,
) -> dict[str, str | int | None]:
    goal = _clip_text(first_user_message, limit=180)
    status = _status_from_activity(activity, error_count)

    scope = repo_name or (project if project and project != "unknown" else None) or source
    summary_bits: list[str] = [f"Worked in {scope}."]

    write_count = activity.get("write_path_count") or 0
    if write_count:
        path_samples = _sample_paths(session_paths)
        path_fragment = f" ({', '.join(path_samples)})" if path_samples else ""
        summary_bits.append(f"Touched {write_count} file{'s' if write_count != 1 else ''}{path_fragment}.")

    for label, total_key, failure_key in (
        ("tests", "test_run_count", "test_failure_count"),
        ("lint checks", "lint_run_count", "lint_failure_count"),
        ("builds", "build_run_count", "build_failure_count"),
    ):
        total = activity.get(total_key) or 0
        failures = activity.get(failure_key) or 0
        if not total:
            continue
        fragment = f"Ran {label} {total} time{'s' if total != 1 else ''}"
        if failures:
            fragment += f" with {failures} failure{'s' if failures != 1 else ''}"
        summary_bits.append(f"{fragment}.")

    commit_count = activity.get("git_commit_count") or 0
    if commit_count:
        summary_bits.append(f"Made {commit_count} git commit{'s' if commit_count != 1 else ''}.")
    elif not write_count and not any(
        (activity.get(key) or 0) > 0
        for key in ("test_run_count", "lint_run_count", "build_run_count")
    ):
        summary_bits.append("Primarily inspected files and commands.")

    if status == "success":
        if commit_count:
            outcome = f"Ended with {commit_count} git commit{'s' if commit_count != 1 else ''}."
        elif write_count:
            outcome = "Ended with code changes and no recorded failing checks."
        else:
            outcome = "Completed without recorded failures."
    elif status == "partial":
        failure_parts = []
        for label, key in (
            ("test", "test_failure_count"),
            ("lint", "lint_failure_count"),
            ("build", "build_failure_count"),
        ):
            count = activity.get(key) or 0
            if count:
                failure_parts.append(f"{count} {label} failure{'s' if count != 1 else ''}")
        joined = ", ".join(failure_parts) if failure_parts else "recorded errors"
        outcome = f"Made progress, but left {joined}."
    elif status == "failed":
        failure_parts = []
        for label, key in (
            ("test", "test_failure_count"),
            ("lint", "lint_failure_count"),
            ("build", "build_failure_count"),
        ):
            count = activity.get(key) or 0
            if count:
                failure_parts.append(f"{count} {label} failure{'s' if count != 1 else ''}")
        joined = ", ".join(failure_parts) if failure_parts else "errors"
        outcome = f"Ended with {joined} and no successful publish signal."
    else:
        outcome = "No decisive code-change outcome was recorded."

    return {
        "session_goal": goal,
        "session_summary": " ".join(summary_bits),
        "session_outcome": outcome,
        "session_status": status,
        "narrative_version": SESSION_NARRATIVE_VERSION,
    }


def _compute_duration(t1: str, t2: str) -> float | None:
    if not t1 or not t2:
        return None
    try:
        from datetime import datetime
        d1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
        d2 = datetime.fromisoformat(t2.replace("Z", "+00:00"))
        return max(0.0, (d2 - d1).total_seconds())
    except Exception:
        return None


def sync_sessions(
    shared_dir: Path,
    db_path: Path,
    username: str,
    machine: str,
    home: Path,
    verbose: bool = False,
) -> tuple[int, int, int]:
    """
    Discover, parse, and copy sessions.
    Returns (new, updated, skipped) counts.

    Holds an exclusive lock for the duration: a concurrent sync (e.g. the
    usage-tracker launchd job overlapping a manual run) returns (0, 0, 0)
    instead of interleaving copies onto the same shared files.
    """
    lock_path = Path(f"{db_path}.sync.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Always audible: a silent (0, 0, 0) reads as "synced, all quiet"
            # to humans and scripts checking the summary line.
            print("Skipped: another logpile sync holds the lock.", file=sys.stderr)
            return (0, 0, 0)
        return _sync_sessions(shared_dir, db_path, username, machine, home, verbose)


def _sync_sessions(
    shared_dir: Path,
    db_path: Path,
    username: str,
    machine: str,
    home: Path,
    verbose: bool = False,
) -> tuple[int, int, int]:
    """Locked body of sync_sessions."""
    init_db(db_path)
    patterns = load_ignore_patterns(home)
    now = datetime.now(timezone.utc).isoformat()
    new_count = updated_count = skipped_count = 0

    with get_db(db_path) as conn:
        canonical_username = _resolve_sync_username(conn, username)
        canonical_username = ensure_user(conn, canonical_username, display_name=canonical_username)
        user_row = conn.execute(
            "SELECT default_session_visibility FROM users WHERE username = ?",
            (canonical_username,),
        ).fetchone()
        default_visibility = (
            user_row["default_session_visibility"] if user_row else "unlisted"
        )
        existing = {
            row["session_id"]: row
            for row in conn.execute(
                """
                SELECT
                    session_id,
                    file_hash,
                    visibility,
                    visibility_source,
                    tool_call_count,
                    activity_version,
                    narrative_version,
                    session_status,
                    session_summary,
                    objective_family,
                    objective_version,
                    session_origin,
                    origin_version,
                    token_version,
                    workspace_root,
                    worktree_root,
                    repo_name,
                    shared_path,
                    source,
                    source_path,
                    project,
                    file_size,
                    file_mtime,
                    (
                        SELECT COUNT(*)
                        FROM session_paths sp
                        WHERE sp.session_id = sessions.session_id
                    ) AS path_count
                FROM sessions
                """
            )
        }
        processed_count = 0
        repo_metadata_cache: dict[str, dict[str, str | int | None]] = {}

        def flush_if_needed() -> None:
            nonlocal processed_count
            processed_count += 1
            if processed_count % 50 == 0:
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        # ── Claude Code sessions ───────────────────────────────────────────────
        claude_root = home / ".claude" / "projects"
        if claude_root.exists():
            for jsonl_path in sorted(claude_root.rglob("*.jsonl")):
                if should_ignore(jsonl_path, patterns):
                    skipped_count += 1
                    continue

                session_id = jsonl_path.stem
                existing_row = existing.get(session_id)
                needs_structure_backfill = bool(
                    existing_row and (
                        (existing_row["workspace_root"] or "") == ""
                        or (existing_row["worktree_root"] or "") == ""
                        or (existing_row["repo_name"] or "") == ""
                        or (existing_row["activity_version"] or 0) < SESSION_ACTIVITY_VERSION
                        or (existing_row["narrative_version"] or 0) < SESSION_NARRATIVE_VERSION
                        or (existing_row["session_status"] or "") == ""
                        or (existing_row["session_summary"] or "") == ""
                        or (existing_row["objective_version"] or 0) < SESSION_OBJECTIVE_VERSION
                        or (existing_row["origin_version"] or 0) < SESSION_ORIGIN_VERSION
                        or (existing_row["token_version"] or 0) < SESSION_TOKEN_VERSION
                        or (existing_row["session_origin"] or "") == ""
                        or (
                            (existing_row["tool_call_count"] or 0) > 0
                            and not existing_row["path_count"]
                        )
                    )
                )

                if existing_row and not needs_structure_backfill and _unchanged_on_disk(existing_row, jsonl_path):
                    skipped_count += 1
                    continue

                try:
                    # stat BEFORE hashing: a write landing mid-hash then makes
                    # the stored size/mtime stale, forcing a re-parse next
                    # sync instead of silently skipping the newer content.
                    file_stat = jsonl_path.stat()
                except OSError:
                    skipped_count += 1
                    continue
                fhash = file_hash(jsonl_path)

                if existing_row and existing_row["file_hash"] == fhash and not needs_structure_backfill:
                    shared_path, storage_changed = _sync_shared_copy(
                        src=jsonl_path,
                        shared_dir=shared_dir,
                        username=canonical_username,
                        source=existing_row["source"],
                        project=existing_row["project"] or project_from_claude_path(jsonl_path),
                        filename=jsonl_path.name,
                        visibility=existing_row["visibility"],
                        existing_shared_path=existing_row["shared_path"],
                    )
                    row_moved = (
                        existing_row["source_path"] != str(jsonl_path)
                        or existing_row["file_size"] != file_stat.st_size
                        or existing_row["file_mtime"] is None
                        or abs(existing_row["file_mtime"] - file_stat.st_mtime) > 1e-6
                    )
                    if storage_changed or row_moved or shared_path != (existing_row["shared_path"] or ""):
                        conn.execute(
                            """
                            UPDATE sessions
                            SET shared_path = ?, source_path = ?, file_size = ?,
                                file_mtime = ?, synced_at = ?
                            WHERE session_id = ?
                            """,
                            (shared_path, str(jsonl_path), file_stat.st_size,
                             file_stat.st_mtime, now, session_id),
                        )
                        flush_if_needed()
                        updated_count += 1
                    else:
                        skipped_count += 1
                    continue

                info = parse_claudecode_session(jsonl_path)
                if info is None:
                    skipped_count += 1
                    continue

                project = project_from_claude_path(jsonl_path)
                workspace_root = _normalize_workspace_root(
                    info.workspace_root or (info.project if info.project != "unknown" else None)
                )
                repo_metadata = _resolve_repo_metadata(workspace_root, repo_metadata_cache)
                session_paths = _annotate_session_paths(
                    info.session_paths,
                    repo_root=repo_metadata["repo_root"],
                    worktree_root=repo_metadata["worktree_root"],
                    workspace_root=workspace_root,
                )
                activity = _derive_session_activity(info.tool_calls, session_paths)
                narrative = _derive_session_narrative(
                    source="claudecode",
                    project=project,
                    repo_name=repo_metadata["repo_name"],
                    first_user_message=info.first_user_message,
                    error_count=info.error_count,
                    activity=activity,
                    session_paths=session_paths,
                )
                origin = derive_session_origin(
                    source="claudecode",
                    session_id=info.session_id,
                    first_user_message=info.first_user_message,
                    project=project,
                    workspace_root=workspace_root,
                    source_path=str(jsonl_path),
                )
                objective = derive_session_objective(
                    narrative["session_goal"],
                    info.first_user_message,
                    narrative["session_summary"],
                )
                visibility = resolve_session_visibility(
                    conn,
                    username=canonical_username,
                    source="claudecode",
                    default_visibility=default_visibility,
                    session_data={
                        "project": project,
                        "source_path": str(jsonl_path),
                        "first_user_message": info.first_user_message,
                        "model": info.model,
                        "machine": machine,
                        "username": canonical_username,
                    },
                )
                storage_visibility = _effective_storage_visibility(existing_row, visibility)
                shared_path, _ = _sync_shared_copy(
                    src=jsonl_path,
                    shared_dir=shared_dir,
                    username=canonical_username,
                    source="claudecode",
                    project=project,
                    filename=jsonl_path.name,
                    visibility=storage_visibility,
                    existing_shared_path=existing_row["shared_path"] if existing_row else None,
                )

                upsert_session(conn, {
                    "session_id": info.session_id,
                    "source": "claudecode",
                    "username": canonical_username,
                    "machine": machine,
                    "project": project,
                    "workspace_root": workspace_root,
                    "worktree_root": repo_metadata["worktree_root"],
                    "repo_root": repo_metadata["repo_root"],
                    "repo_name": repo_metadata["repo_name"],
                    "git_branch": repo_metadata["git_branch"],
                    "git_commit": repo_metadata["git_commit"],
                    "git_dirty": repo_metadata["git_dirty"],
                    "source_path": str(jsonl_path),
                    "shared_path": shared_path,
                    "first_timestamp": info.first_timestamp,
                    "last_timestamp": info.last_timestamp,
                    "duration_seconds": _compute_duration(info.first_timestamp, info.last_timestamp),
                    "user_message_count": info.user_message_count,
                    "assistant_message_count": info.assistant_message_count,
                    "tool_call_count": info.tool_call_count,
                    "error_count": info.error_count,
                    "total_input_tokens": info.total_input_tokens,
                    "total_output_tokens": info.total_output_tokens,
                    "fresh_input_tokens": info.fresh_input_tokens,
                    "cached_input_tokens": info.cached_input_tokens,
                    "reasoning_output_tokens": info.reasoning_output_tokens,
                    "token_version": SESSION_TOKEN_VERSION,
                    "first_user_message": info.first_user_message,
                    "parent_session_id": info.parent_session_id,
                    "spawn_depth": info.spawn_depth,
                    "visibility": visibility["visibility"],
                    "visibility_source": visibility["visibility_source"],
                    "visibility_rule_id": visibility["visibility_rule_id"],
                    "visibility_reason": visibility["visibility_reason"],
                    "is_private": 1 if visibility["visibility"] == "private" else 0,
                    "file_hash": fhash,
                    "file_size": file_stat.st_size,
                    "file_mtime": file_stat.st_mtime,
                    "synced_at": now,
                    "model": info.model,
                    **activity,
                    **narrative,
                    **objective,
                    **origin,
                })
                insert_tool_calls(conn, info.session_id, info.tool_calls)
                insert_session_paths(conn, info.session_id, session_paths)
                insert_session_daily_usage(conn, info.session_id, info.daily_usage)
                flush_if_needed()

                action = "Updated" if session_id in existing else "Added"
                if existing_row:
                    updated_count += 1
                else:
                    new_count += 1

                if verbose:
                    print(f"  {action}: {session_id[:12]}… ({project})")

        # ── Codex sessions ─────────────────────────────────────────────────────
        seen_codex_stems: set[str] = set()
        for codex_root in _codex_session_roots(home):
            for jsonl_path in sorted(codex_root.rglob("*.jsonl")):
                if should_ignore(jsonl_path, patterns):
                    skipped_count += 1
                    continue

                session_id = jsonl_path.stem  # full filename stem as unique ID
                if session_id in seen_codex_stems:
                    skipped_count += 1
                    continue
                seen_codex_stems.add(session_id)
                existing_row = existing.get(session_id)
                needs_structure_backfill = bool(
                    existing_row and (
                        (existing_row["workspace_root"] or "") == ""
                        or (existing_row["worktree_root"] or "") == ""
                        or (existing_row["repo_name"] or "") == ""
                        or (existing_row["activity_version"] or 0) < SESSION_ACTIVITY_VERSION
                        or (existing_row["narrative_version"] or 0) < SESSION_NARRATIVE_VERSION
                        or (existing_row["session_status"] or "") == ""
                        or (existing_row["session_summary"] or "") == ""
                        or (existing_row["objective_version"] or 0) < SESSION_OBJECTIVE_VERSION
                        or (existing_row["origin_version"] or 0) < SESSION_ORIGIN_VERSION
                        or (existing_row["token_version"] or 0) < SESSION_TOKEN_VERSION
                        or (existing_row["session_origin"] or "") == ""
                        or (
                            (existing_row["tool_call_count"] or 0) > 0
                            and not existing_row["path_count"]
                        )
                    )
                )

                if existing_row and not needs_structure_backfill and _unchanged_on_disk(existing_row, jsonl_path):
                    skipped_count += 1
                    continue

                try:
                    # stat BEFORE hashing — see the Claude loop.
                    file_stat = jsonl_path.stat()
                except OSError:
                    skipped_count += 1
                    continue
                fhash = file_hash(jsonl_path)

                if existing_row and existing_row["file_hash"] == fhash and not needs_structure_backfill:
                    shared_path, storage_changed = _sync_shared_copy(
                        src=jsonl_path,
                        shared_dir=shared_dir,
                        username=canonical_username,
                        source=existing_row["source"],
                        project=existing_row["project"] or "unknown",
                        filename=jsonl_path.name,
                        visibility=existing_row["visibility"],
                        existing_shared_path=existing_row["shared_path"],
                    )
                    row_moved = (
                        existing_row["source_path"] != str(jsonl_path)
                        or existing_row["file_size"] != file_stat.st_size
                        or existing_row["file_mtime"] is None
                        or abs(existing_row["file_mtime"] - file_stat.st_mtime) > 1e-6
                    )
                    if storage_changed or row_moved or shared_path != (existing_row["shared_path"] or ""):
                        conn.execute(
                            """
                            UPDATE sessions
                            SET shared_path = ?, source_path = ?, file_size = ?,
                                file_mtime = ?, synced_at = ?
                            WHERE session_id = ?
                            """,
                            (shared_path, str(jsonl_path), file_stat.st_size,
                             file_stat.st_mtime, now, session_id),
                        )
                        flush_if_needed()
                        updated_count += 1
                    else:
                        skipped_count += 1
                    continue

                info = parse_codex_session(jsonl_path)
                if info is None:
                    skipped_count += 1
                    continue

                # Use leaf of cwd as project name
                project = Path(info.project).name if info.project != "unknown" else "unknown"
                workspace_root = _normalize_workspace_root(
                    info.workspace_root or (info.project if info.project != "unknown" else None)
                )
                repo_metadata = _resolve_repo_metadata(workspace_root, repo_metadata_cache)
                session_paths = _annotate_session_paths(
                    info.session_paths,
                    repo_root=repo_metadata["repo_root"],
                    worktree_root=repo_metadata["worktree_root"],
                    workspace_root=workspace_root,
                )
                activity = _derive_session_activity(info.tool_calls, session_paths)
                narrative = _derive_session_narrative(
                    source="codex",
                    project=project,
                    repo_name=repo_metadata["repo_name"],
                    first_user_message=info.first_user_message,
                    error_count=info.error_count,
                    activity=activity,
                    session_paths=session_paths,
                )
                origin = derive_session_origin(
                    source="codex",
                    session_id=session_id,
                    first_user_message=info.first_user_message,
                    project=project,
                    workspace_root=workspace_root,
                    source_path=str(jsonl_path),
                )
                objective = derive_session_objective(
                    narrative["session_goal"],
                    info.first_user_message,
                    narrative["session_summary"],
                )
                visibility = resolve_session_visibility(
                    conn,
                    username=canonical_username,
                    source="codex",
                    default_visibility=default_visibility,
                    session_data={
                        "project": project,
                        "source_path": str(jsonl_path),
                        "first_user_message": info.first_user_message,
                        "model": info.model,
                        "machine": machine,
                        "username": canonical_username,
                    },
                )
                storage_visibility = _effective_storage_visibility(existing_row, visibility)
                shared_path, _ = _sync_shared_copy(
                    src=jsonl_path,
                    shared_dir=shared_dir,
                    username=canonical_username,
                    source="codex",
                    project=project,
                    filename=jsonl_path.name,
                    visibility=storage_visibility,
                    existing_shared_path=existing_row["shared_path"] if existing_row else None,
                )

                # Use file_stem as session_id (unique per file)
                upsert_session(conn, {
                    "session_id": session_id,
                    "source": "codex",
                    "username": canonical_username,
                    "machine": machine,
                    "project": project,
                    "workspace_root": workspace_root,
                    "worktree_root": repo_metadata["worktree_root"],
                    "repo_root": repo_metadata["repo_root"],
                    "repo_name": repo_metadata["repo_name"],
                    "git_branch": repo_metadata["git_branch"],
                    "git_commit": repo_metadata["git_commit"],
                    "git_dirty": repo_metadata["git_dirty"],
                    "source_path": str(jsonl_path),
                    "shared_path": shared_path,
                    "first_timestamp": info.first_timestamp,
                    "last_timestamp": info.last_timestamp,
                    "duration_seconds": None,
                    "user_message_count": info.user_message_count,
                    "assistant_message_count": info.assistant_message_count,
                    "tool_call_count": info.tool_call_count,
                    "error_count": info.error_count,
                    "total_input_tokens": info.total_input_tokens,
                    "total_output_tokens": info.total_output_tokens,
                    "fresh_input_tokens": info.fresh_input_tokens,
                    "cached_input_tokens": info.cached_input_tokens,
                    "reasoning_output_tokens": info.reasoning_output_tokens,
                    "token_version": SESSION_TOKEN_VERSION,
                    "first_user_message": info.first_user_message,
                    "parent_session_id": info.parent_session_id,
                    "spawn_depth": info.spawn_depth,
                    "visibility": visibility["visibility"],
                    "visibility_source": visibility["visibility_source"],
                    "visibility_rule_id": visibility["visibility_rule_id"],
                    "visibility_reason": visibility["visibility_reason"],
                    "is_private": 1 if visibility["visibility"] == "private" else 0,
                    "file_hash": fhash,
                    "file_size": file_stat.st_size,
                    "file_mtime": file_stat.st_mtime,
                    "synced_at": now,
                    "model": info.model,
                    **activity,
                    **narrative,
                    **objective,
                    **origin,
                })
                insert_tool_calls(conn, session_id, info.tool_calls)
                insert_session_paths(conn, session_id, session_paths)
                insert_session_daily_usage(conn, session_id, info.daily_usage)
                flush_if_needed()

                action = "Updated" if session_id in existing else "Added"
                if existing_row:
                    updated_count += 1
                else:
                    new_count += 1

                if verbose:
                    short = session_id[-20:] if len(session_id) > 20 else session_id
                    print(f"  {action}: …{short} ({project})")

    return new_count, updated_count, skipped_count
