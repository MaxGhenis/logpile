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
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TranscriptRoot:
    """One transcript root and the traversal policy that applies to it.

    ``anchor`` is ``None`` for the fixed ambient roots, which keep their
    historical traversal. Dynamically discovered managed roots (Subfleet lane
    homes and numbered ``~/.codex-N`` homes) carry the trusted directory they
    were discovered under: every component from ``anchor`` down to ``path``
    must be a real directory, and nothing below ``path`` is followed through
    a symlink.
    """

    path: Path
    source: str
    anchor: Path | None = None


@dataclass(frozen=True)
class DiscoveredTranscript:
    path: Path
    source: str


# Subfleet v1 found extra Codex subscriptions at exactly ``~/.codex-1`` through
# ``~/.codex-9`` (``paths.codex_homes``). Subfleet v2 renames a home it takes
# over to ``$SUBFLEET_HOME/lanes/<lane id>``, and its Codex lane ids are
# ``codex-<suffix>`` with a numeric suffix. Only these names are lane homes, so
# dated backups such as ``~/.codex-20260915`` and config directories are never
# traversed. Lanes that Subfleet enrolled in place elsewhere are not discovered.
_LEGACY_CODEX_HOME = re.compile(r"\.codex-(?P<lane>[1-9])")
_SUBFLEET_CODEX_LANE = re.compile(r"codex-(?P<lane>[0-9]+)")


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _is_real_directory(path: Path) -> bool:
    """Return whether ``path`` is a directory reached without a leaf symlink."""

    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _real_directory_below(anchor: Path, path: Path) -> bool:
    """Return whether ``path`` is reached from ``anchor`` through real directories.

    ``anchor`` is trusted: like a home directory, it may itself be a symlink.
    Every component below it, ``path`` included, must be a directory that is
    not a symlink, so a swapped-in link cannot redirect discovery elsewhere.
    """

    anchor = _absolute(anchor)
    path = _absolute(path)
    try:
        relative = path.relative_to(anchor)
    except ValueError:
        return False
    try:
        if not stat.S_ISDIR(anchor.stat().st_mode):
            return False
        current = anchor
        for component in relative.parts:
            current = current / component
            if not stat.S_ISDIR(current.lstat().st_mode):
                return False
    except OSError:
        return False
    return True


# Problems already reported, so each is printed once per process however often
# sync and backup rebuild the roots.
_REPORTED: set[object] = set()


def _warn_once(key: object, message: str) -> None:
    if key in _REPORTED:
        return
    _REPORTED.add(key)
    print(f"Warning: {message}", file=sys.stderr)


def _report_unreadable(directory: Path) -> None:
    _warn_once(
        ("unreadable", directory),
        f"cannot read {directory}; Codex transcripts below it are skipped.",
    )


def _report_hidden(anchor: Path, path: Path, *, state_root: Path | None = None) -> None:
    """Warn once when a symlink or unreadable directory hides ``path``.

    A lane must never drop out of sync and backup unseen. Missing directories
    are the normal case (no Subfleet, no archive yet) and stay silent.
    """

    anchor = _absolute(anchor)
    try:
        relative = _absolute(path).relative_to(anchor)
    except ValueError:
        return
    current = anchor
    for component in relative.parts:
        parent, current = current, current / component
        try:
            mode = current.lstat().st_mode
        except PermissionError:
            _report_unreadable(parent)
            return
        except OSError:
            return
        if stat.S_ISLNK(mode):
            if state_root is not None and current == _absolute(state_root):
                advice = "set SUBFLEET_HOME to the directory it points to"
            else:
                advice = "replace it with the real directory"
            _warn_once(
                ("symlink", current),
                f"not following symlink {current} to Codex transcripts; "
                f"{advice} to include them.",
            )
            return
        if not stat.S_ISDIR(mode):
            return


def _numbered_lane_homes(
    anchor: Path,
    parent: Path,
    pattern: re.Pattern[str],
    *,
    state_root: Path | None = None,
) -> tuple[Path, ...]:
    """Return ``parent``'s real lane-home children in numeric lane order."""

    if not _real_directory_below(anchor, parent):
        _report_hidden(anchor, parent, state_root=state_root)
        return ()
    try:
        children = tuple(parent.iterdir())
    except PermissionError:
        _report_unreadable(_absolute(parent))
        return ()
    except OSError:
        return ()
    lanes: list[tuple[int, str, Path]] = []
    for path in children:
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        if not _is_real_directory(path):
            _report_hidden(anchor, path, state_root=state_root)
            continue
        lanes.append((int(match.group("lane")), path.name, path))
    lanes.sort(key=lambda item: (item[0], item[1]))
    return tuple(path for _, _, path in lanes)


def _codex_home_roots(
    anchor: Path,
    homes: tuple[Path, ...],
    *,
    state_root: Path | None = None,
) -> tuple[TranscriptRoot, ...]:
    """Return each managed Codex home's live root, then its archive root.

    A Codex home also holds credentials and configuration, so only its exact
    ``sessions`` and ``archived_sessions`` children are ever admitted.
    """

    roots: list[TranscriptRoot] = []
    for home in homes:
        for name, source in (
            ("sessions", "codex"),
            ("archived_sessions", "codex_archive"),
        ):
            path = home / name
            if _real_directory_below(anchor, path):
                roots.append(TranscriptRoot(path, source, anchor=anchor))
            else:
                _report_hidden(anchor, path, state_root=state_root)
    return tuple(roots)


def _is_current_user_home(home: Path) -> bool:
    try:
        current = Path.home()
    except RuntimeError:
        return False
    try:
        return os.path.samefile(home, current)
    except OSError:
        return _absolute(home) == _absolute(current)


def subfleet_state_root(home: Path) -> tuple[Path, Path]:
    """Return Subfleet's state root for ``home`` and the anchor its lanes trust.

    Subfleet takes its state root from ``SUBFLEET_HOME`` (default
    ``~/.subfleet``) and resolves it with ``expanduser().resolve()``. The
    variable describes the current user's environment, so it applies only when
    ``home`` is that user's home; scanning any other home (``logpile backup
    --home``) uses that home's ``.subfleet``. A configured value is resolved
    the same way and, because the user chose it, becomes the trusted anchor. A
    relative value, whose meaning depends on the reading process's working
    directory, or a ``~user`` that cannot be expanded, is ignored with a
    warning. The default ``.subfleet`` is validated from ``home`` down.
    """

    home = _absolute(home)
    default = (home / ".subfleet", home)
    configured = os.environ.get("SUBFLEET_HOME", "")
    if not configured or not _is_current_user_home(home):
        return default
    try:
        expanded: Path | None = Path(configured).expanduser()
    except RuntimeError:
        expanded = None
    if expanded is None or not expanded.is_absolute():
        _warn_once(
            ("SUBFLEET_HOME", configured),
            f"ignoring SUBFLEET_HOME={configured!r}; it is not an absolute "
            f"path, so Subfleet lanes are read from {default[0]}.",
        )
        return default
    state_root = Path(os.path.realpath(expanded))
    return state_root, state_root


def transcript_roots(home: Path) -> tuple[TranscriptRoot, ...]:
    """Return every supported transcript root in deterministic priority order.

    Sync keeps the first copy of a session ID, so order is precedence. Each
    Codex home contributes its live ``sessions`` root and then its
    ``archived_sessions`` root, so a live rollout wins a mid-archive race
    within its home. Homes come in this order: the ambient ``~/.codex``,
    Subfleet v2 lanes, legacy ``~/.codex-<n>`` homes (each by lane number),
    then OpenClaw homes. A rollout appears in two homes only when a home was
    copied rather than moved (Subfleet moves a lane between the two layouts
    with one rename), and the earlier home wins. Standard ambient roots are
    returned even while absent; managed roots must already be real
    directories below their anchor.
    """

    # One absolute spelling for validation, traversal and reported paths.
    home = _absolute(home)
    state_root, state_anchor = subfleet_state_root(home)
    lanes = _numbered_lane_homes(
        state_anchor,
        state_root / "lanes",
        _SUBFLEET_CODEX_LANE,
        state_root=state_root,
    )
    roots = [
        TranscriptRoot(home / ".claude" / "projects", "claudecode"),
        TranscriptRoot(home / ".codex" / "sessions", "codex"),
        TranscriptRoot(home / ".codex" / "archived_sessions", "codex_archive"),
        *_codex_home_roots(state_anchor, lanes, state_root=state_root),
        *_codex_home_roots(home, _numbered_lane_homes(home, home, _LEGACY_CODEX_HOME)),
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

    return _absolute(home) / ".claude" / "projects"


def codex_session_roots(home: Path) -> tuple[Path, ...]:
    """Return all Codex rollout roots with live/archive priority preserved."""

    return tuple(root.path for root in codex_transcript_roots(home))


def codex_transcript_roots(home: Path) -> tuple[TranscriptRoot, ...]:
    """Return Codex rollout roots with source and traversal policy."""

    return tuple(
        root for root in transcript_roots(home) if root.source.startswith("codex")
    )


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


def _report_walk_error(error: OSError) -> None:
    if isinstance(error, PermissionError) and error.filename:
        _report_unreadable(_absolute(Path(error.filename)))


def _managed_transcript_files(root: Path, anchor: Path) -> list[Path]:
    """Walk a managed root without following symlinks, anchor to leaf."""

    if not _real_directory_below(anchor, root):
        _report_hidden(anchor, root)
        return []
    candidates: list[Path] = []
    for directory, child_directories, filenames in os.walk(
        root, topdown=True, onerror=_report_walk_error, followlinks=False
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
            if _safe_managed_file(candidate, root):
                candidates.append(candidate)
    # os.walk trusts each path it is handed. If an ancestor is a symlink once
    # the walk ends, the files it listed may live elsewhere: admit none. A swap
    # undone before this check goes unseen, and callers reopen files by path.
    # These checks stop stray and misconfigured links, not a concurrent writer.
    if not _real_directory_below(anchor, root):
        _report_hidden(anchor, root)
        return []
    return sorted(candidates)


def iter_transcript_files(root: TranscriptRoot) -> Iterator[Path]:
    """Yield a root's JSONL transcripts in stable path order.

    Ambient provider roots retain their historical traversal behavior. Managed
    roots are checked from their anchor down before and after a non-following
    walk, and every file is revalidated below the root, so a transcript-looking
    symlink or a symlinked ancestor cannot pull in credential or configuration
    files from elsewhere. Files are reopened by path afterwards: this guards
    against stray and misconfigured links, not a process racing the scan.
    """

    if root.anchor is not None:
        yield from _managed_transcript_files(root.path, root.anchor)
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
