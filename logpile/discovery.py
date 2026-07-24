"""Canonical discovery of durable agent transcript files.

Sync and cloud backup must agree on every native rollout root. Backup also
consults the local catalog for managed shared, private-archive, and reviewed
copies. A source can rotate away or its pathname can be reused for new bytes,
leaving a managed artifact as the only copy of an indexed revision.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TranscriptRoot:
    path: Path
    source: str


@dataclass(frozen=True)
class DiscoveredTranscript:
    path: Path
    source: str


def transcript_roots(home: Path) -> tuple[TranscriptRoot, ...]:
    """Return every supported transcript root in deterministic priority order.

    Primary Codex live sessions precede archives and alternate homes so sync's
    existing session-stem collision behavior remains stable.  Standard roots
    are returned even while absent: Codex can create or rotate into one during
    an active discovery pass.
    """

    home = Path(home)
    roots = [
        TranscriptRoot(home / ".claude" / "projects", "claudecode"),
        TranscriptRoot(home / ".codex" / "sessions", "codex"),
        TranscriptRoot(home / ".codex" / "archived_sessions", "codex_archive"),
        TranscriptRoot(home / ".codex-2" / "sessions", "codex"),
        TranscriptRoot(home / ".codex-3" / "sessions", "codex"),
    ]
    openclaw_agents = home / ".openclaw" / "agents"
    if openclaw_agents.exists():
        roots.extend(
            TranscriptRoot(path, "codex")
            for path in sorted(openclaw_agents.glob("*/agent/codex-home/sessions"))
        )
    return tuple(roots)


def claude_projects_root(home: Path) -> Path:
    """Return the canonical Claude Code projects root."""

    return Path(home) / ".claude" / "projects"


def codex_session_roots(home: Path) -> tuple[Path, ...]:
    """Return all Codex rollout roots with live/archive priority preserved."""

    return tuple(
        root.path for root in transcript_roots(home) if root.source.startswith("codex")
    )


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _safe_managed_file(path: Path, root: Path) -> bool:
    """Accept only regular files reached without symlinks below ``root``."""

    path = _absolute(path)
    root = _absolute(root)
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False

    current = root
    try:
        root_mode = current.lstat().st_mode
        if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
            return False
        for index, component in enumerate(relative.parts):
            current = current / component
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                return False
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(mode):
                return False
        return bool(relative.parts) and stat.S_ISREG(current.lstat().st_mode)
    except OSError:
        return False


def _db_shared_transcripts(
    db_path: Path | None,
    shared_dir: Path | None,
) -> Iterator[DiscoveredTranscript]:
    if db_path is None or shared_dir is None:
        return
    db_path = _absolute(db_path)
    shared_dir = _absolute(shared_dir)
    private_dir = shared_dir.parent / f".{shared_dir.name}-private"
    try:
        db_mode = db_path.stat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(
            "Could not inspect configured Logpile database for backup artifact "
            f"discovery ({db_path}): {exc}"
        ) from exc
    if not stat.S_ISREG(db_mode):
        raise RuntimeError(
            "Could not read configured Logpile database for backup artifact "
            f"discovery: {db_path} is not a regular file"
        )
    managed_roots = (shared_dir, private_dir)

    try:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            if not {"source", "source_path", "shared_path"}.issubset(columns):
                return
            reviewed_column = (
                "reviewed_artifact_path"
                if "reviewed_artifact_path" in columns
                else "NULL AS reviewed_artifact_path"
            )
            reviewed_filter = (
                "OR COALESCE(reviewed_artifact_path, '') != ''"
                if "reviewed_artifact_path" in columns
                else ""
            )
            rows = conn.execute(
                f"""
                SELECT source, source_path, shared_path, {reviewed_column}
                FROM sessions
                WHERE COALESCE(shared_path, '') != ''
                   {reviewed_filter}
                ORDER BY session_id
                """
            )
            for row in rows:
                source = row["source"]
                if source not in {"claudecode", "codex", "codex_archive"}:
                    source = "other"
                # Always enumerate managed DB artifacts.  A source pathname can
                # be reused for a newer revision, while the archival/reviewed
                # bytes remain the only copy of the older indexed revision.
                # Backup's full-SHA pass removes true byte duplicates.
                for raw_path in (
                    row["shared_path"],
                    row["reviewed_artifact_path"],
                ):
                    if not raw_path:
                        continue
                    artifact_path = Path(raw_path)
                    if not any(
                        _safe_managed_file(artifact_path, root)
                        for root in managed_roots
                    ):
                        continue
                    yield DiscoveredTranscript(_absolute(artifact_path), source)
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        # A missing database and a valid SQLite database without Logpile's
        # sessions schema are optional.  Once a configured database exists,
        # however, read/query failures must stop backup planning: otherwise a
        # managed artifact that survives only in shared storage is silently
        # omitted from the backup.
        raise RuntimeError(
            "Could not read configured Logpile database for backup artifact "
            f"discovery ({db_path}): {exc}"
        ) from exc


def discover_transcripts(
    home: Path,
    *,
    db_path: Path | None = None,
    shared_dir: Path | None = None,
) -> Iterator[DiscoveredTranscript]:
    """Yield native transcripts plus every safe DB-managed artifact."""

    seen_paths: set[Path] = set()
    for root in transcript_roots(home):
        if not root.path.exists():
            continue
        for path in sorted(
            candidate for candidate in root.path.rglob("*.jsonl") if candidate.is_file()
        ):
            absolute = _absolute(path)
            if absolute in seen_paths:
                continue
            seen_paths.add(absolute)
            yield DiscoveredTranscript(absolute, root.source)

    for transcript in _db_shared_transcripts(db_path, shared_dir):
        if transcript.path in seen_paths:
            continue
        seen_paths.add(transcript.path)
        yield transcript
