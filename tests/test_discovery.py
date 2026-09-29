import contextlib
import io
import itertools
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from logpile import discovery
from logpile.discovery import (
    TranscriptRoot,
    discover_transcripts,
    iter_transcript_files,
    subfleet_state_root,
    transcript_roots,
)


def write_file(path: Path, text: str = '{"type": "session_meta"}\n') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def discovered(home: Path) -> list[Path]:
    return [transcript.path for transcript in discover_transcripts(home)]


def discovered_with_warnings(home: Path) -> tuple[list[Path], str]:
    stream = io.StringIO()
    with contextlib.redirect_stderr(stream):
        paths = discovered(home)
    return paths, stream.getvalue()


def managed_roots(home: Path) -> list[tuple[Path, str, Path | None]]:
    return [
        (root.path, root.source, root.anchor)
        for root in transcript_roots(home)
        if root.anchor is not None
    ]


def real(path: Path) -> Path:
    return Path(os.path.realpath(path))


class SubfleetLaneDiscoveryTests(unittest.TestCase):
    def test_each_home_lists_live_then_archive_in_precedence_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            lanes = home / ".subfleet" / "lanes"
            openclaw = home / ".openclaw" / "agents" / "bot" / "agent" / "codex-home"
            for directory in (
                lanes / "codex-10" / "sessions",
                lanes / "codex-2" / "archived_sessions",
                lanes / "codex-2" / "sessions",
                # Equal lane numbers fall back to the name, whatever the
                # directory listing order.
                lanes / "codex-1" / "sessions",
                lanes / "codex-01" / "sessions",
                home / ".codex-3" / "sessions",
                home / ".codex-3" / "archived_sessions",
                home / ".codex-9" / "sessions",
                openclaw / "sessions",
                # Not Codex lane homes: Claude lanes, API-style names, and
                # backups. Subfleet v1 used exactly ~/.codex-1 to ~/.codex-9.
                lanes / "claude-2" / "sessions",
                lanes / "codex-api" / "sessions",
                lanes / "codex-2-old" / "sessions",
                home / ".codex-backup" / "sessions",
                home / ".codex-" / "sessions",
                home / ".codex-0" / "sessions",
                home / ".codex-02" / "sessions",
                home / ".codex-10" / "sessions",
                home / ".codex-20260915" / "sessions",
            ):
                directory.mkdir(parents=True)

            self.assertEqual(
                [(root.path, root.source) for root in transcript_roots(home)],
                [
                    (home / ".claude" / "projects", "claudecode"),
                    (home / ".codex" / "sessions", "codex"),
                    (home / ".codex" / "archived_sessions", "codex_archive"),
                    (lanes / "codex-01" / "sessions", "codex"),
                    (lanes / "codex-1" / "sessions", "codex"),
                    (lanes / "codex-2" / "sessions", "codex"),
                    (lanes / "codex-2" / "archived_sessions", "codex_archive"),
                    (lanes / "codex-10" / "sessions", "codex"),
                    (home / ".codex-3" / "sessions", "codex"),
                    (home / ".codex-3" / "archived_sessions", "codex_archive"),
                    (home / ".codex-9" / "sessions", "codex"),
                    (openclaw / "sessions", "codex"),
                ],
            )
            self.assertTrue(all(anchor == home for _, _, anchor in managed_roots(home)))

    def test_symlinked_managed_ancestors_admit_nothing(self) -> None:
        """Each directory from home down to a managed root must be real.

        The symlink targets hold a complete, valid-looking layout, so following
        any of these links would admit a transcript from outside home.
        """

        v2_live = ".subfleet/lanes/codex-2/sessions/2026/live.jsonl"
        v2_archive = ".subfleet/lanes/codex-2/archived_sessions/old.jsonl"
        legacy_live = ".codex-2/sessions/2026/live.jsonl"
        legacy_archive = ".codex-2/archived_sessions/old.jsonl"
        relocate = "set SUBFLEET_HOME to the directory it points to"
        replace = "replace it with the real directory"
        cases = {
            ".subfleet": ((".subfleet",), v2_live, relocate),
            "lanes": ((".subfleet", "lanes"), v2_live, replace),
            "v2 lane home": ((".subfleet", "lanes", "codex-2"), v2_live, replace),
            "v2 sessions": (
                (".subfleet", "lanes", "codex-2", "sessions"),
                v2_live,
                replace,
            ),
            "v2 archive": (
                (".subfleet", "lanes", "codex-2", "archived_sessions"),
                v2_archive,
                replace,
            ),
            "legacy home": ((".codex-2",), legacy_live, replace),
            "legacy sessions": ((".codex-2", "sessions"), legacy_live, replace),
            "legacy archive": (
                (".codex-2", "archived_sessions"),
                legacy_archive,
                replace,
            ),
        }
        for label, (component, transcript, advice) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                home = base / "home"
                home.mkdir()
                # A real lane in the other layout must still be discovered.
                if component[0] == ".subfleet":
                    control = home / ".codex-7" / "sessions" / "c.jsonl"
                else:
                    control = (
                        home
                        / ".subfleet"
                        / "lanes"
                        / "codex-7"
                        / "sessions"
                        / "c.jsonl"
                    )
                write_file(control)
                outside = base / "outside"
                write_file(outside.joinpath(*Path(transcript).parts[len(component) :]))
                link = home.joinpath(*component)
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside, target_is_directory=True)

                paths, warnings = discovered_with_warnings(home)
                self.assertEqual(paths, [control])
                self.assertEqual(
                    warnings.count(f"not following symlink {link} "), 1, warnings
                )
                self.assertIn(advice, warnings)
                # Reported once per process, however often roots are rebuilt.
                self.assertEqual(discovered_with_warnings(home), ([control], ""))
                self.assertFalse(
                    any(
                        path == link or link in path.parents
                        for path, _, _ in managed_roots(home)
                    )
                )

    def test_absent_managed_directories_are_silent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self.assertEqual(discovered_with_warnings(home), ([], ""))
            (home / ".subfleet" / "lanes" / "codex-1").mkdir(parents=True)
            (home / ".codex-2").mkdir()
            (home / ".subfleet" / "lanes" / "codex-3").write_text("not a lane")
            self.assertEqual(discovered_with_warnings(home), ([], ""))

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unreadable_lane_directories_are_reported(self) -> None:
        cases = {
            "state root": (".subfleet",),
            "lanes": (".subfleet", "lanes"),
            "v2 lane home": (".subfleet", "lanes", "codex-1"),
            "v2 sessions": (".subfleet", "lanes", "codex-1", "sessions"),
            "legacy home": (".codex-2",),
            "legacy sessions": (".codex-2", "sessions"),
        }
        for label, component in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as td:
                home = Path(td)
                files = [
                    write_file(home / ".codex" / "sessions" / "c.jsonl"),
                    write_file(
                        home
                        / ".subfleet"
                        / "lanes"
                        / "codex-1"
                        / "sessions"
                        / "a.jsonl"
                    ),
                    write_file(home / ".codex-2" / "sessions" / "b.jsonl"),
                ]
                locked = home.joinpath(*component)
                locked.chmod(0)
                try:
                    paths, warnings = discovered_with_warnings(home)
                finally:
                    locked.chmod(0o700)
                self.assertEqual(
                    paths, [path for path in files if locked not in path.parents]
                )
                self.assertEqual(warnings.count(f"cannot read {locked};"), 1, warnings)

    def test_symlinked_home_remains_a_trusted_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            real_home = base / "real-home"
            write_file(real_home / ".codex-2" / "sessions" / "legacy.jsonl")
            write_file(
                real_home / ".subfleet" / "lanes" / "codex-1" / "sessions" / "v2.jsonl"
            )
            home = base / "home-link"
            home.symlink_to(real_home, target_is_directory=True)

            self.assertEqual(
                discovered(home),
                [
                    home / ".subfleet" / "lanes" / "codex-1" / "sessions" / "v2.jsonl",
                    home / ".codex-2" / "sessions" / "legacy.jsonl",
                ],
            )

    def test_tilde_home_is_expanded_for_every_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            claude = write_file(home / ".claude" / "projects" / "-p" / "c.jsonl")
            codex = write_file(home / ".codex" / "sessions" / "x.jsonl")
            lane = write_file(
                home / ".subfleet" / "lanes" / "codex-1" / "sessions" / "l.jsonl"
            )
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                self.assertEqual(discovered(Path("~")), [claude, codex, lane])
                self.assertEqual(
                    discovery.claude_projects_root(Path("~")),
                    home / ".claude" / "projects",
                )

    def test_ancestor_swapped_for_symlink_during_walk_admits_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            lanes = home / ".subfleet" / "lanes"
            transcript = write_file(lanes / "codex-1" / "sessions" / "a.jsonl")
            (root,) = [r for r in transcript_roots(home) if r.anchor is not None]
            self.assertEqual(list(iter_transcript_files(root)), [transcript])

            real_walk = os.walk
            moved = base / "moved-lanes"

            def walk_then_swap(top, **kwargs):
                yield from real_walk(top, **kwargs)
                if not moved.exists():
                    lanes.rename(moved)
                    lanes.symlink_to(moved, target_is_directory=True)

            with (
                mock.patch.object(discovery.os, "walk", walk_then_swap),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(list(iter_transcript_files(root)), [])
            self.assertTrue(lanes.is_symlink())

    def test_root_whose_ancestor_became_a_symlink_is_never_walked(self) -> None:
        """Rejecting results afterward is not enough: a link to ``/`` would
        still enumerate the whole filesystem."""

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            lanes = home / ".subfleet" / "lanes"
            write_file(lanes / "codex-1" / "sessions" / "a.jsonl")
            (root,) = [r for r in transcript_roots(home) if r.anchor is not None]

            moved = base / "moved-lanes"
            lanes.rename(moved)
            lanes.symlink_to(moved, target_is_directory=True)
            with (
                mock.patch.object(
                    discovery.os, "walk", side_effect=AssertionError("walked")
                ) as walk,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(list(iter_transcript_files(root)), [])
            walk.assert_not_called()

    def test_file_and_directory_symlinks_below_a_lane_root_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            lane = home / ".subfleet" / "lanes" / "codex-3"
            kept = write_file(lane / "sessions" / "2026" / "kept.jsonl")
            credentials = write_file(lane / "credentials.jsonl")
            history = write_file(lane / "history" / "secret.jsonl")
            (lane / "sessions" / "linked.jsonl").symlink_to(credentials)
            (lane / "sessions" / "linked-dir").symlink_to(
                history.parent, target_is_directory=True
            )

            self.assertEqual(discovered(home), [kept])

    def test_only_regular_files_named_jsonl_are_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            for sessions in (
                home / ".subfleet" / "lanes" / "codex-1" / "sessions",
                home / ".codex" / "sessions",
                home / ".claude" / "projects" / "-p",
            ):
                with self.subTest(sessions.relative_to(home)):
                    kept = write_file(sessions / "kept.jsonl")
                    nested = write_file(sessions / "directory.jsonl" / "nested.jsonl")
                    os.mkfifo(sessions / "fifo.jsonl")
                    # A FIFO would block a reader forever; it is never yielded.
                    self.assertEqual(
                        [path for path in discovered(home) if sessions in path.parents],
                        [nested, kept],
                    )


class SubfleetHomeTests(unittest.TestCase):
    def _env(self, home: Path, subfleet_home: str | None) -> dict[str, str]:
        env = {"HOME": str(home)}
        if subfleet_home is not None:
            env["SUBFLEET_HOME"] = subfleet_home
        return env

    def test_default_state_root_is_dot_subfleet_under_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            for value in (None, ""):
                with (
                    self.subTest(subfleet_home=value),
                    mock.patch.dict(os.environ, self._env(home, value)),
                ):
                    if value is None:
                        os.environ.pop("SUBFLEET_HOME", None)
                    self.assertEqual(
                        subfleet_state_root(home), (home / ".subfleet", home)
                    )

    def test_configured_state_root_is_resolved_and_trusted(self) -> None:
        """Subfleet resolves SUBFLEET_HOME, so Logpile reads the same lanes."""

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            home.mkdir()
            real_state = base / "volume" / "subfleet"
            write_file(real_state / "lanes" / "codex-4" / "sessions" / "a.jsonl")
            outside_link = base / "subfleet-link"
            outside_link.symlink_to(real_state, target_is_directory=True)
            inside_link = home / ".subfleet-link"
            inside_link.symlink_to(real_state, target_is_directory=True)
            write_file(
                home / ".subfleet" / "lanes" / "codex-9" / "sessions" / "x.jsonl"
            )
            resolved = real(real_state)

            for configured in (
                str(real_state),
                str(outside_link),
                "~/.subfleet-link",
                f"{base}/volume/../volume/subfleet",
            ):
                with (
                    self.subTest(configured),
                    mock.patch.dict(os.environ, self._env(home, configured)),
                ):
                    self.assertEqual(subfleet_state_root(home), (resolved, resolved))
                    self.assertEqual(
                        discovered(home),
                        [resolved / "lanes" / "codex-4" / "sessions" / "a.jsonl"],
                    )

            # Below the configured anchor the usual rule applies.
            (real_state / "lanes").rename(base / "lanes-elsewhere")
            (real_state / "lanes").symlink_to(
                base / "lanes-elsewhere", target_is_directory=True
            )
            with mock.patch.dict(os.environ, self._env(home, str(real_state))):
                paths, warnings = discovered_with_warnings(home)
            self.assertEqual(paths, [])
            self.assertIn(f"not following symlink {resolved / 'lanes'} ", warnings)

    def test_unusable_state_root_is_ignored_with_a_warning(self) -> None:
        for configured in ("relative-state", "~logpile-no-such-user/state"):
            with self.subTest(configured), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                home = base / "home"
                write_file(
                    base
                    / "relative-state"
                    / "lanes"
                    / "codex-1"
                    / "sessions"
                    / "r.jsonl"
                )
                default = write_file(
                    home / ".subfleet" / "lanes" / "codex-2" / "sessions" / "d.jsonl"
                )
                cwd = Path.cwd()
                os.chdir(base)
                stderr = io.StringIO()
                try:
                    with (
                        mock.patch.dict(os.environ, self._env(home, configured)),
                        contextlib.redirect_stderr(stderr),
                    ):
                        self.assertEqual(
                            subfleet_state_root(home), (home / ".subfleet", home)
                        )
                        paths = discovered(home)
                finally:
                    os.chdir(cwd)
                self.assertEqual(paths, [default])
                self.assertEqual(
                    stderr.getvalue().count(f"ignoring SUBFLEET_HOME={configured!r}"),
                    1,
                )

    def test_configured_state_root_applies_to_home_spelled_another_way(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            real_home = base / "real-home"
            real_home.mkdir()
            home_link = base / "home-link"
            home_link.symlink_to(real_home, target_is_directory=True)
            state = base / "state"
            transcript = write_file(
                state / "lanes" / "codex-1" / "sessions" / "a.jsonl"
            )

            with mock.patch.dict(os.environ, self._env(real_home, str(state))):
                self.assertEqual(discovered(home_link), [real(transcript)])

    def test_configured_state_root_does_not_apply_to_another_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            current_home = base / "current"
            current_home.mkdir()
            configured = base / "configured-state"
            write_file(configured / "lanes" / "codex-1" / "sessions" / "mine.jsonl")
            other_home = base / "mounted-home"
            theirs = write_file(
                other_home / ".subfleet" / "lanes" / "codex-1" / "sessions" / "t.jsonl"
            )

            with mock.patch.dict(os.environ, self._env(current_home, str(configured))):
                self.assertEqual(
                    subfleet_state_root(other_home),
                    (other_home / ".subfleet", other_home),
                )
                self.assertEqual(discovered(other_home), [theirs])


# ── Properties ────────────────────────────────────────────────────────────────
#
# Invariants of discovery, for every generated tree:
#   1. Differential: discover_transcripts equals a reference model built from
#      the tree's specification, in order. A managed transcript is admitted iff
#      it is a ``.jsonl`` under the exact ``sessions``/``archived_sessions``
#      child of a home named exactly ``.codex-<1-9>`` (legacy) or
#      ``<state root>/lanes/codex-<n>`` (v2), and no component below its anchor,
#      the file included, is a symlink. The anchor is home, or the resolved
#      SUBFLEET_HOME, and may itself be a symlink. Ambient
#      ``~/.claude/projects`` and ``~/.codex`` files are admitted as before.
#      Roots order: claudecode, then each Codex home's live root followed by
#      its archive root, homes ordered ambient, v2 lanes, legacy homes, each
#      by lane number and then name.
#   2. Containment: every admitted path is a regular file whose real path is
#      its anchor's real path joined with its path below the anchor.
#   3. Anchor trust: making home itself a symlink changes nothing but the
#      prefix of the admitted paths.
#   4. Determinism: repeated discovery over an unchanged tree is identical.

_AMBIENT_ROOTS = [
    (".claude", "projects", "-tmp-demo"),
    (".codex", "sessions"),
    (".codex", "archived_sessions"),
]
_LEGACY_HOMES = [
    ".codex-1",
    ".codex-2",
    ".codex-9",
    ".codex-10",
    ".codex-0",
    ".codex-02",
    ".codex-",
    ".codex-x",
]
_V2_LANES = [
    "codex-1",
    "codex-2",
    "codex-10",
    "codex-02",
    "codex-",
    "codex-api",
    "claude-1",
]
# Repeated entries weight generation toward admissible transcripts, so most
# examples mix several roots and exercise their relative order.
_SUBDIRS = [
    "sessions",
    "sessions",
    "archived_sessions",
    "archived_sessions",
    "logs",
    None,
]
_NESTS = [(), ("2026",), ("2026", "04")]
_NAMES = ["a.jsonl", "b.jsonl", "c.jsonl", "auth.json", "d.jsonl.bak"]


@st.composite
def _file_specs(draw: st.DrawFn) -> tuple[str, ...]:
    kind = draw(st.sampled_from(["v2", "legacy", "ambient"]))
    name = draw(st.sampled_from(_NAMES))
    if kind == "ambient":
        return (
            *draw(st.sampled_from(_AMBIENT_ROOTS)),
            *draw(st.sampled_from(_NESTS)),
            name,
        )
    lane = draw(st.sampled_from(_V2_LANES if kind == "v2" else _LEGACY_HOMES))
    subdir = draw(st.sampled_from(_SUBDIRS))
    nest = draw(st.sampled_from(_NESTS)) if subdir is not None else ()
    prefix = (".subfleet", "lanes") if kind == "v2" else ()
    return (*prefix, lane, *((subdir,) if subdir else ()), *nest, name)


@st.composite
def _trees(draw: st.DrawFn):
    files = draw(st.lists(_file_specs(), min_size=2, max_size=16, unique=True))
    # Ambient roots keep their historical traversal, which the model does not
    # cover for symlinks, so links are placed only on managed paths.
    prefixes = sorted(
        {
            spec[:end]
            for spec in files
            if spec[0] not in {".claude", ".codex"}
            for end in range(1, len(spec) + 1)
        }
    )
    links = (
        draw(st.frozensets(st.sampled_from(prefixes), max_size=3))
        if prefixes
        else frozenset()
    )
    return files, links


class _Tree:
    """A generated home, plus the configured Subfleet state root if any."""

    def __init__(self, home: Path, state: Path | None) -> None:
        self.home = home
        self.state = state

    def anchor_for(self, spec: tuple[str, ...]) -> Path:
        if self.state is not None and spec[0] == ".subfleet":
            return real(self.state)
        return self.home

    def spec_path(self, spec: tuple[str, ...]) -> Path:
        if self.state is not None and spec[0] == ".subfleet":
            return self.anchor_for(spec).joinpath(*spec[1:])
        return self.home.joinpath(*spec)

    @contextlib.contextmanager
    def environment(self):
        if self.state is None:
            yield
            return
        env = {"HOME": str(self.home), "SUBFLEET_HOME": str(self.state)}
        with mock.patch.dict(os.environ, env):
            yield


def _build_tree(
    base: Path,
    files: list[tuple[str, ...]],
    links: frozenset[tuple[str, ...]],
    *,
    home_is_symlink: bool,
    external_state: bool = False,
) -> _Tree:
    """Create ``files``, replacing each ``links`` prefix by a symlink.

    A linked prefix points at a real directory (or file) elsewhere that holds
    the same subtree, so an implementation that followed it would find
    admissible-looking transcripts. With ``external_state`` the ``.subfleet``
    prefix lives outside home and is configured through SUBFLEET_HOME; linking
    that prefix makes the configured state root itself a symlink.
    """

    real_home = base / "real-home"
    real_home.mkdir()
    outside = base / "outside"
    outside.mkdir()
    counter = itertools.count()
    location: dict[tuple[str, ...], Path] = {(): real_home}
    state = None
    if external_state:
        state = real_state = base / "state"
        real_state.mkdir()
        location[(".subfleet",)] = real_state
        if (".subfleet",) in links:
            state = base / "state-link"
            state.symlink_to(real_state, target_is_directory=True)

    def directory(prefix: tuple[str, ...]) -> Path:
        if prefix not in location:
            parent = directory(prefix[:-1])
            if prefix in links:
                target = outside / f"dir-{next(counter)}"
                target.mkdir()
                (parent / prefix[-1]).symlink_to(target, target_is_directory=True)
                location[prefix] = target
            else:
                (parent / prefix[-1]).mkdir()
                location[prefix] = parent / prefix[-1]
        return location[prefix]

    for spec in files:
        parent = directory(spec[:-1])
        content = '{"type": "session_meta"}\n'
        if spec in links:
            target = outside / f"file-{next(counter)}.jsonl"
            target.write_text(content, encoding="utf-8")
            (parent / spec[-1]).symlink_to(target)
        else:
            (parent / spec[-1]).write_text(content, encoding="utf-8")

    home = real_home
    if home_is_symlink:
        home = base / "home-link"
        home.symlink_to(real_home, target_is_directory=True)
    return _Tree(home, state)


def _reference_discovery(
    tree: _Tree,
    files: list[tuple[str, ...]],
    links: frozenset[tuple[str, ...]],
) -> list[tuple[Path, str]]:
    roots: dict[tuple[str, ...], list[Path]] = {}
    for spec in files:
        if not spec[-1].endswith(".jsonl"):
            continue
        if spec[0] in {".claude", ".codex"}:
            root = spec[:2]
        else:
            # A configured state root is a trusted anchor, like home.
            trusted = 1 if tree.state is not None and spec[0] == ".subfleet" else 0
            if any(spec[:end] in links for end in range(trusted + 1, len(spec) + 1)):
                continue
            if spec[0] == ".subfleet":
                if len(spec) < 5 or not re.fullmatch(r"codex-[0-9]+", spec[2]):
                    continue
                root = spec[:4]
            else:
                if len(spec) < 3 or not re.fullmatch(r"\.codex-[1-9]", spec[0]):
                    continue
                root = spec[:2]
            if root[-1] not in {"sessions", "archived_sessions"}:
                continue
        roots.setdefault(root, []).append(tree.spec_path(spec))

    def precedence(root: tuple[str, ...]) -> tuple[int, int, int, str, int]:
        live_then_archive = 0 if root[-1] == "sessions" else 1
        if root[0] == ".claude":
            return (0, 0, 0, "", 0)
        if root[0] == ".codex":
            return (1, 0, 0, "", live_then_archive)
        lane = root[-2]
        layout = 1 if root[0] == ".subfleet" else 2
        number = int(lane.rsplit("-", 1)[1])
        return (1, layout, number, lane, live_then_archive)

    def source(root: tuple[str, ...]) -> str:
        if root[0] == ".claude":
            return "claudecode"
        return "codex" if root[-1] == "sessions" else "codex_archive"

    return [
        (path, source(root))
        for root in sorted(roots, key=precedence)
        for path in sorted(roots[root])
    ]


_PROPERTY_SETTINGS = {
    "deadline": None,
    "suppress_health_check": [HealthCheck.too_slow],
}


class ManagedDiscoveryPropertyTests(unittest.TestCase):
    @settings(max_examples=150, **_PROPERTY_SETTINGS)
    @given(
        tree=_trees(),
        home_is_symlink=st.booleans(),
        external_state=st.booleans(),
    )
    def test_discovery_matches_reference_model(
        self, tree, home_is_symlink, external_state
    ) -> None:
        files, links = tree
        with (
            tempfile.TemporaryDirectory() as td,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            built = _build_tree(
                Path(td),
                files,
                links,
                home_is_symlink=home_is_symlink,
                external_state=external_state,
            )
            with built.environment():
                actual = [(t.path, t.source) for t in discover_transcripts(built.home)]
                self.assertEqual(actual, _reference_discovery(built, files, links))

                anchors = {
                    built.spec_path(spec): built.anchor_for(spec) for spec in files
                }
                for path, _ in actual:
                    anchor = anchors[path]
                    self.assertTrue(stat.S_ISREG(path.lstat().st_mode))
                    self.assertEqual(
                        real(path), real(anchor) / path.relative_to(anchor)
                    )

                self.assertEqual(
                    [(t.path, t.source) for t in discover_transcripts(built.home)],
                    actual,
                )

    @settings(max_examples=60, **_PROPERTY_SETTINGS)
    @given(tree=_trees())
    def test_symlinked_home_changes_only_the_path_prefix(self, tree) -> None:
        files, links = tree
        with (
            tempfile.TemporaryDirectory() as direct_td,
            tempfile.TemporaryDirectory() as linked_td,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            direct = _build_tree(
                Path(direct_td), files, links, home_is_symlink=False
            ).home
            linked = _build_tree(
                Path(linked_td), files, links, home_is_symlink=True
            ).home
            self.assertEqual(
                [
                    (t.path.relative_to(direct), t.source)
                    for t in discover_transcripts(direct)
                ],
                [
                    (t.path.relative_to(linked), t.source)
                    for t in discover_transcripts(linked)
                ],
            )

    @settings(max_examples=60, **_PROPERTY_SETTINGS)
    @given(tree=_trees())
    def test_each_home_keeps_live_before_archive_and_stays_together(self, tree) -> None:
        files, links = tree
        with (
            tempfile.TemporaryDirectory() as td,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            home = _build_tree(Path(td), files, links, home_is_symlink=False).home
            roots = transcript_roots(home)
            self.assertEqual(roots[0].source, "claudecode")
            codex_homes = [root.path.parent for root in roots[1:]]
            # A home's roots are adjacent: once left, a home never returns.
            finished: set[Path] = set()
            for previous, current in itertools.pairwise(codex_homes):
                if current != previous:
                    finished.add(previous)
                self.assertNotIn(current, finished)
            for index, root in enumerate(roots):
                self.assertIsInstance(root, TranscriptRoot)
                if root.anchor is not None:
                    self.assertEqual(root.anchor, home)
                    self.assertIn(root.path.name, {"sessions", "archived_sessions"})
                if root.source == "codex_archive":
                    self.assertEqual(root.path.name, "archived_sessions")
                    live = root.path.parent / "sessions"
                    if any(other.path == live for other in roots):
                        self.assertEqual(roots[index - 1].path, live)


if __name__ == "__main__":
    unittest.main()
