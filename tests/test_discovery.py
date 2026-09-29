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


class SubfleetLaneDiscoveryTests(unittest.TestCase):
    def test_roots_follow_live_then_archive_and_v2_then_legacy_precedence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            lanes = home / ".subfleet" / "lanes"
            for directory in (
                lanes / "codex-10" / "sessions",
                lanes / "codex-2" / "sessions",
                lanes / "codex-2" / "archived_sessions",
                home / ".codex-3" / "sessions",
                home / ".codex-3" / "archived_sessions",
                home / ".codex-10" / "sessions",
                # Not Codex lane homes: Claude lanes, API-style names, and
                # backups never match the exact numbered names.
                lanes / "claude-2" / "sessions",
                lanes / "codex-api" / "sessions",
                lanes / "codex-2-old" / "sessions",
                home / ".codex-backup" / "sessions",
                home / ".codex-" / "sessions",
            ):
                directory.mkdir(parents=True)

            self.assertEqual(
                [(root.path, root.source) for root in transcript_roots(home)],
                [
                    (home / ".claude" / "projects", "claudecode"),
                    (home / ".codex" / "sessions", "codex"),
                    (lanes / "codex-2" / "sessions", "codex"),
                    (lanes / "codex-10" / "sessions", "codex"),
                    (home / ".codex-3" / "sessions", "codex"),
                    (home / ".codex-10" / "sessions", "codex"),
                    (home / ".codex" / "archived_sessions", "codex_archive"),
                    (lanes / "codex-2" / "archived_sessions", "codex_archive"),
                    (home / ".codex-3" / "archived_sessions", "codex_archive"),
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
        cases = {
            ".subfleet": ((".subfleet",), v2_live),
            "lanes": ((".subfleet", "lanes"), v2_live),
            "v2 lane home": ((".subfleet", "lanes", "codex-2"), v2_live),
            "v2 sessions": ((".subfleet", "lanes", "codex-2", "sessions"), v2_live),
            "v2 archive": (
                (".subfleet", "lanes", "codex-2", "archived_sessions"),
                v2_archive,
            ),
            "legacy home": ((".codex-2",), legacy_live),
            "legacy sessions": ((".codex-2", "sessions"), legacy_live),
            "legacy archive": ((".codex-2", "archived_sessions"), legacy_archive),
        }
        for label, (component, transcript) in cases.items():
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

            with mock.patch.object(discovery.os, "walk", walk_then_swap):
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
            with mock.patch.object(
                discovery.os, "walk", side_effect=AssertionError("walked")
            ) as walk:
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
            sessions = home / ".subfleet" / "lanes" / "codex-1" / "sessions"
            kept = write_file(sessions / "kept.jsonl")
            (sessions / "directory.jsonl").mkdir()
            write_file(sessions / "directory.jsonl" / "nested.jsonl")
            os.mkfifo(sessions / "fifo.jsonl")

            self.assertEqual(
                discovered(home),
                [sessions / "directory.jsonl" / "nested.jsonl", kept],
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

    def test_configured_state_root_outside_home_is_its_own_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            home.mkdir()
            real_state = base / "volume" / "subfleet"
            transcript = write_file(
                real_state / "lanes" / "codex-4" / "sessions" / "a.jsonl"
            )
            # Subfleet resolves its own state root, so a configured symlink is
            # trusted exactly like a symlinked home.
            state_link = base / "subfleet-link"
            state_link.symlink_to(real_state, target_is_directory=True)
            write_file(
                home / ".subfleet" / "lanes" / "codex-9" / "sessions" / "x.jsonl"
            )

            with mock.patch.dict(os.environ, self._env(home, str(state_link))):
                self.assertEqual(subfleet_state_root(home), (state_link, state_link))
                self.assertEqual(
                    discovered(home),
                    [state_link / "lanes" / "codex-4" / "sessions" / "a.jsonl"],
                )
                self.assertEqual(discovered(home)[0].resolve(), transcript.resolve())

                # Below the configured anchor the usual rule applies.
                (real_state / "lanes").rename(base / "lanes-elsewhere")
                (real_state / "lanes").symlink_to(
                    base / "lanes-elsewhere", target_is_directory=True
                )
                self.assertEqual(discovered(home), [])

    def test_configured_state_root_inside_home_is_validated_from_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            write_file(
                home / ".subfleet-alt" / "lanes" / "codex-1" / "sessions" / "a.jsonl"
            )
            state = home / ".subfleet-alt"
            with mock.patch.dict(os.environ, self._env(home, "~/.subfleet-alt")):
                self.assertEqual(subfleet_state_root(home), (state, home))
                self.assertEqual(
                    discovered(home),
                    [state / "lanes" / "codex-1" / "sessions" / "a.jsonl"],
                )

                state.rename(base / "elsewhere")
                state.symlink_to(base / "elsewhere", target_is_directory=True)
                self.assertEqual(discovered(home), [])

    def test_relative_state_root_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            write_file(
                base / "relative-state" / "lanes" / "codex-1" / "sessions" / "r.jsonl"
            )
            default = write_file(
                home / ".subfleet" / "lanes" / "codex-2" / "sessions" / "d.jsonl"
            )
            cwd = Path.cwd()
            os.chdir(base)
            try:
                with mock.patch.dict(os.environ, self._env(home, "relative-state")):
                    self.assertEqual(
                        subfleet_state_root(home), (home / ".subfleet", home)
                    )
                    self.assertEqual(discovered(home), [default])
            finally:
                os.chdir(cwd)

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
#      child of a home named exactly ``.codex-<n>`` (legacy) or
#      ``<state root>/lanes/codex-<n>`` (v2), and no component below its anchor,
#      the file included, is a symlink. The anchor is home, or a SUBFLEET_HOME
#      outside home, and may itself be a symlink. Ambient ``~/.claude/projects``
#      and ``~/.codex`` files are admitted as before. Roots order claudecode,
#      then live before archive; within a tier ambient, v2, legacy, each by
#      lane number.
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
    ".codex-10",
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

    def spec_path(self, spec: tuple[str, ...]) -> Path:
        if self.state is not None and spec[0] == ".subfleet":
            return self.state.joinpath(*spec[1:])
        return self.home.joinpath(*spec)

    def anchor_for(self, spec: tuple[str, ...]) -> Path:
        if self.state is not None and spec[0] == ".subfleet":
            return self.state
        return self.home

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
                if len(spec) < 3 or not re.fullmatch(r"\.codex-[0-9]+", spec[0]):
                    continue
                root = spec[:2]
            if root[-1] not in {"sessions", "archived_sessions"}:
                continue
        roots.setdefault(root, []).append(tree.spec_path(spec))

    def precedence(root: tuple[str, ...]) -> tuple[int, int, int, int, str]:
        if root == (".claude", "projects"):
            return (0, 0, 0, 0, "")
        tier = 1 if root[-1] == "sessions" else 2
        if root[0] == ".codex":
            return (tier, 0, 0, 0, "")
        lane = root[-2]
        layout = 1 if root[0] == ".subfleet" else 2
        return (tier, layout, int(lane.rsplit("-", 1)[1]), 0, lane)

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
                        Path(os.path.realpath(path)),
                        Path(os.path.realpath(anchor)) / path.relative_to(anchor),
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
    def test_every_live_root_precedes_every_archive_root(self, tree) -> None:
        files, links = tree
        with (
            tempfile.TemporaryDirectory() as td,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            home = _build_tree(Path(td), files, links, home_is_symlink=False).home
            roots = transcript_roots(home)
            sources = [root.source for root in roots]
            self.assertEqual(sources[0], "claudecode")
            codex = [s for s in sources if s.startswith("codex")]
            self.assertEqual(codex, sorted(codex, key=lambda s: s == "codex_archive"))
            for root in roots:
                self.assertIsInstance(root, TranscriptRoot)
                if root.anchor is not None:
                    self.assertEqual(root.anchor, home)
                    self.assertIn(root.path.name, {"sessions", "archived_sessions"})


if __name__ == "__main__":
    unittest.main()
