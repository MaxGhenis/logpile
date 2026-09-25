"""Canonical discovery of durable agent transcript files.

Sync and cloud backup must agree on every native rollout root. Backup also
consults the local catalog for managed shared, private-archive, and reviewed
copies. A source can rotate away or its pathname can be reused for new bytes,
leaving a managed artifact as the only copy of an indexed revision.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TranscriptRoot:
    path: Path
    source: str
    reject_symlinks: bool = False


@dataclass(frozen=True)
class DiscoveredTranscript:
    path: Path
    source: str


_NUMBERED_CODEX_HOME = re.compile(r"\.codex-(?P<lane>[0-9]+)\Z")


def _is_real_directory(path: Path) -> bool:
    """Return whether ``path`` is a directory reached without a leaf symlink."""

    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _direct_real_directories(parent: Path) -> tuple[Path, ...]:
    """List only immediate, non-symlink directory children deterministically."""

    if not _is_real_directory(parent):
        return ()
    try:
        children = tuple(parent.iterdir())
    except OSError:
        return ()
    return tuple(
        sorted(
            (path for path in children if _is_real_directory(path)),
            key=lambda path: path.name,
        )
    )


def _numbered_codex_roots(home: Path) -> tuple[TranscriptRoot, ...]:
    """Return exact ``.codex-N`` lane transcript roots in numeric order.

    Subfleet gives each additional Codex subscription an isolated numbered
    ``CODEX_HOME``. Match the complete directory name so similarly named
    backups or config directories are never traversed.
    """

    lanes: list[tuple[int, str, Path]] = []
    for path in _direct_real_directories(home):
        match = _NUMBERED_CODEX_HOME.fullmatch(path.name)
        if match is None:
            continue
        lanes.append((int(match.group("lane")), path.name, path))
    lanes.sort(key=lambda item: (item[0], item[1]))

    live = [
        TranscriptRoot(path / "sessions", "codex", reject_symlinks=True)
        for _, _, path in lanes
        if _is_real_directory(path / "sessions")
    ]
    archived = [
        TranscriptRoot(
            path / "archived_sessions", "codex_archive", reject_symlinks=True
        )
        for _, _, path in lanes
        if _is_real_directory(path / "archived_sessions")
    ]
    return (*live, *archived)


def _traycer_profile_roots(home: Path) -> tuple[TranscriptRoot, ...]:
    """Return exact transcript subdirectories for Traycer-managed profiles.

    A managed profile's directory is also its provider config home. Enumerate
    profile directories only one level deep, then admit the provider's exact
    transcript subdirectory. Credential and configuration siblings are never
    recursively scanned.
    """

    accounts_root = home / ".traycer" / "harness-accounts"
    claude_profiles = _direct_real_directories(accounts_root / "claude-code")
    codex_profiles = _direct_real_directories(accounts_root / "codex")

    claude = [
        TranscriptRoot(profile / "projects", "claudecode", reject_symlinks=True)
        for profile in claude_profiles
        if _is_real_directory(profile / "projects")
    ]
    codex_live = [
        TranscriptRoot(profile / "sessions", "codex", reject_symlinks=True)
        for profile in codex_profiles
        if _is_real_directory(profile / "sessions")
    ]
    codex_archived = [
        TranscriptRoot(
            profile / "archived_sessions", "codex_archive", reject_symlinks=True
        )
        for profile in codex_profiles
        if _is_real_directory(profile / "archived_sessions")
    ]
    return (*claude, *codex_live, *codex_archived)


def transcript_roots(home: Path) -> tuple[TranscriptRoot, ...]:
    """Return every supported transcript root in deterministic priority order.

    Every Codex live root precedes every archive root so a live rollout wins a
    session-stem collision even when it moves between managed homes. Standard
    ambient roots are returned even while absent; dynamically managed roots
    must already be real directories and may not be symlinks.
    """

    home = Path(home)
    numbered = _numbered_codex_roots(home)
    traycer = _traycer_profile_roots(home)
    roots = [TranscriptRoot(home / ".claude" / "projects", "claudecode")]
    roots.extend(root for root in traycer if root.source == "claudecode")
    roots.append(TranscriptRoot(home / ".codex" / "sessions", "codex"))
    roots.extend(root for root in numbered if root.source == "codex")
    openclaw_agents = home / ".openclaw" / "agents"
    if openclaw_agents.exists():
        roots.extend(
            TranscriptRoot(path, "codex")
            for path in sorted(openclaw_agents.glob("*/agent/codex-home/sessions"))
        )
    roots.extend(root for root in traycer if root.source == "codex")
    roots.append(TranscriptRoot(home / ".codex" / "archived_sessions", "codex_archive"))
    roots.extend(root for root in numbered if root.source == "codex_archive")
    roots.extend(root for root in traycer if root.source == "codex_archive")
    return tuple(roots)


def claude_projects_root(home: Path) -> Path:
    """Return the canonical Claude Code projects root."""

    return Path(home) / ".claude" / "projects"


def claude_project_roots(home: Path) -> tuple[Path, ...]:
    """Return ambient and managed Claude Code transcript roots."""

    return tuple(root.path for root in claude_transcript_roots(home))


def claude_transcript_roots(home: Path) -> tuple[TranscriptRoot, ...]:
    """Return ambient and managed Claude roots with traversal policy."""

    return tuple(root for root in transcript_roots(home) if root.source == "claudecode")


def codex_session_roots(home: Path) -> tuple[Path, ...]:
    """Return all Codex rollout roots with live/archive priority preserved."""

    return tuple(root.path for root in codex_transcript_roots(home))


def codex_transcript_roots(home: Path) -> tuple[TranscriptRoot, ...]:
    """Return Codex rollout roots with source and traversal policy."""

    return tuple(
        root for root in transcript_roots(home) if root.source.startswith("codex")
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


def iter_transcript_files(root: TranscriptRoot) -> Iterator[Path]:
    """Yield a root's JSONL transcripts in stable path order.

    Ambient provider roots retain their historical traversal behavior. Dynamic
    Subfleet and Traycer roots use a non-following walk and revalidate every
    component before admission, preventing a transcript-looking symlink from
    escaping into credential or configuration siblings.
    """

    if root.reject_symlinks:
        if not _is_real_directory(root.path):
            return
        candidates: list[Path] = []
        for directory, child_directories, filenames in os.walk(
            root.path, topdown=True, followlinks=False
        ):
            directory_path = Path(directory)
            child_directories[:] = sorted(
                name
                for name in child_directories
                if _is_real_directory(directory_path / name)
            )
            for filename in filenames:
                if not filename.endswith(".jsonl"):
                    continue
                candidate = directory_path / filename
                if _safe_managed_file(candidate, root.path):
                    candidates.append(candidate)
        yield from sorted(candidates)
        return

    if not root.path.exists():
        return
    yield from sorted(
        candidate for candidate in root.path.rglob("*.jsonl") if candidate.is_file()
    )


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
        for path in iter_transcript_files(root):
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
