"""APFS clone-first copies and the one-time reclone-shared migration.

Cross-platform cases drive the clone path through a stand-in for clonefile(2)
(an O_EXCL byte copy that inherits the source mode, like the real call), so
Linux CI exercises the same staging, verification, and cleanup logic.  Cases
marked darwin-only use the real clonefile(2) and APFS block accounting.
"""

import ctypes
import errno
import fcntl
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from logpile import apfs
from logpile import reclone as reclone_module
from logpile import sync as sync_module
from logpile.cli import cli
from logpile.db import init_db
from logpile.reclone import RecloneLockContended, reclone_shared_copies
from logpile.sync import (
    StorageSafetyError,
    _clone_to_temporary_sibling,
    _secure_copy_file,
    sync_lock,
)

REAL_CLONEFILE = apfs.clonefile
DARWIN_CLONE = sys.platform == "darwin" and apfs.clone_available()
needs_darwin_clone = unittest.skipUnless(
    DARWIN_CLONE, "real clonefile(2) requires macOS on APFS"
)


def fake_clonefile(src, dst) -> None:
    """Stand-in for clonefile(2): follows src, O_EXCL dst, inherits src mode."""
    mode = os.stat(src).st_mode & 0o777
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(fd, mode)
        with open(src, "rb") as source:
            while chunk := source.read(1024 * 1024):
                os.write(fd, chunk)
    finally:
        os.close(fd)


@contextmanager
def simulated_clone(side_effect=fake_clonefile):
    """Force the clone path on any platform; yields the clonefile spy."""
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(apfs, "clone_available", return_value=True)
        )
        spy = stack.enter_context(
            mock.patch.object(apfs, "clonefile", side_effect=side_effect)
        )
        yield spy


def forbid_byte_copy():
    return mock.patch.object(
        sync_module.shutil,
        "copyfileobj",
        side_effect=AssertionError("byte-copy fallback must not run"),
    )


def staging_leftovers(directory: Path) -> list[str]:
    return sorted(name for name in os.listdir(directory) if name.endswith(".tmp-sync"))


class CloneCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.src = self.root / "src" / "session.jsonl"
        self.src.parent.mkdir()
        self.payload = os.urandom(256 * 1024)
        self.src.write_bytes(self.payload)
        self.shared = self.root / "shared"
        self.dst = self.shared / "alice" / "claudecode" / "demo" / "session.jsonl"

    def _copy(self) -> None:
        _secure_copy_file(self.src, self.dst, shared_root=self.shared)

    def test_clone_helper_is_used_when_available(self) -> None:
        with simulated_clone() as spy, forbid_byte_copy():
            self._copy()

        spy.assert_called_once()
        cloned_from, staged = spy.call_args.args
        self.assertEqual(Path(cloned_from), self.src)
        self.assertEqual(Path(staged).parent, self.dst.parent)
        self.assertTrue(Path(staged).name.startswith(f".{self.dst.name}."))
        self.assertTrue(Path(staged).name.endswith(".tmp-sync"))
        self.assertEqual(self.dst.read_bytes(), self.payload)
        self.assertEqual(self.dst.stat().st_mode & 0o777, 0o600)
        self.assertEqual(staging_leftovers(self.dst.parent), [])

    @needs_darwin_clone
    def test_darwin_uses_real_clonefile_and_shares_blocks(self) -> None:
        self.src.chmod(0o644)
        with (
            mock.patch.object(apfs, "clonefile", wraps=REAL_CLONEFILE) as spy,
            forbid_byte_copy(),
        ):
            self._copy()

        spy.assert_called_once()
        self.assertEqual(self.dst.read_bytes(), self.payload)
        self.assertEqual(self.dst.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.dst.stat().st_ino, self.src.stat().st_ino)
        # A clone owns no private blocks until one side is written.
        self.assertEqual(apfs.private_size(self.dst), 0)
        self.assertEqual(staging_leftovers(self.dst.parent), [])

    def test_clone_lands_in_private_directory_and_ends_0600(self) -> None:
        self.src.chmod(0o644)
        self.dst.parent.mkdir(parents=True)
        directory = self.dst.parent
        while directory != self.root:
            directory.chmod(0o755)
            directory = directory.parent
        observed: list[tuple[int, int]] = []

        def clone_and_observe(src, dst):
            fake_clonefile(src, dst)
            observed.append(
                (
                    os.stat(Path(dst).parent).st_mode & 0o777,
                    os.lstat(dst).st_mode & 0o777,
                )
            )

        old_umask = os.umask(0o022)
        try:
            with simulated_clone(clone_and_observe), forbid_byte_copy():
                self._copy()
        finally:
            os.umask(old_umask)

        # The clone inherits 0644, but only inside a 0700 directory.
        self.assertEqual(observed, [(0o700, 0o644)])
        self.assertEqual(self.dst.stat().st_mode & 0o777, 0o600)

    def test_fallback_when_clone_reports_unsupported(self) -> None:
        for error in sorted(apfs.CLONE_UNSUPPORTED_ERRNOS):
            with self.subTest(errno=errno.errorcode.get(error, error)):
                self.dst.unlink(missing_ok=True)
                unsupported = apfs.CloneUnsupported("no clones here", error)
                with (
                    simulated_clone(unsupported) as spy,
                    mock.patch.object(
                        sync_module.shutil,
                        "copyfileobj",
                        wraps=sync_module.shutil.copyfileobj,
                    ) as byte_copy,
                ):
                    self._copy()
                spy.assert_called_once()
                byte_copy.assert_called_once()
                self.assertEqual(self.dst.read_bytes(), self.payload)
                self.assertEqual(self.dst.stat().st_mode & 0o777, 0o600)
                self.assertEqual(staging_leftovers(self.dst.parent), [])

    def test_fallback_when_clone_is_unavailable(self) -> None:
        with (
            mock.patch.object(apfs, "clone_available", return_value=False),
            mock.patch.object(
                apfs, "clonefile", side_effect=AssertionError("must not clone")
            ),
        ):
            self.src.chmod(0o644)
            self._copy()
        self.assertEqual(self.dst.read_bytes(), self.payload)
        self.assertEqual(self.dst.stat().st_mode & 0o777, 0o600)

    def test_other_clone_errors_fail_closed(self) -> None:
        self.dst.parent.mkdir(parents=True)
        self.dst.write_bytes(b"previous complete copy\n")
        with (
            simulated_clone(OSError(errno.EIO, "I/O error")),
            forbid_byte_copy(),
            self.assertRaises(OSError) as raised,
        ):
            self._copy()
        self.assertEqual(raised.exception.errno, errno.EIO)
        self.assertEqual(self.dst.read_bytes(), b"previous complete copy\n")
        self.assertEqual(staging_leftovers(self.dst.parent), [])

    def test_failure_after_clone_removes_staging_file(self) -> None:
        self.dst.parent.mkdir(parents=True)
        self.dst.write_bytes(b"previous complete copy\n")
        for target in ("fsync", "replace"):
            with self.subTest(failing=target):
                with (
                    simulated_clone(),
                    forbid_byte_copy(),
                    mock.patch.object(
                        sync_module.os, target, side_effect=OSError(errno.EIO, target)
                    ),
                    self.assertRaises(OSError),
                ):
                    self._copy()
                self.assertEqual(self.dst.read_bytes(), b"previous complete copy\n")
                self.assertEqual(staging_leftovers(self.dst.parent), [])

    def test_copy_is_independent_of_later_source_changes(self) -> None:
        # Native path: a real clone on macOS, the byte copy elsewhere.
        with mock.patch.object(apfs, "clonefile", wraps=REAL_CLONEFILE) as spy:
            self._copy()
        self.assertEqual(spy.called, DARWIN_CLONE)

        with self.src.open("ab") as source:
            source.write(b'{"appended": true}\n')
        self.assertEqual(self.dst.read_bytes(), self.payload)
        self.assertNotEqual(self.src.read_bytes(), self.payload)

        self.src.unlink()
        self.assertEqual(self.dst.read_bytes(), self.payload)

    def test_symlinked_source_is_followed(self) -> None:
        link = self.root / "src" / "link.jsonl"
        link.symlink_to(self.src)
        with simulated_clone() as spy, forbid_byte_copy():
            _secure_copy_file(link, self.dst, shared_root=self.shared)
        spy.assert_called_once()
        self.assertFalse(self.dst.is_symlink())
        self.assertEqual(self.dst.read_bytes(), self.payload)

    def test_directory_source_is_rejected_without_debris(self) -> None:
        directory = self.root / "src" / "not-a-file.jsonl"
        directory.mkdir()
        (directory / "inner").write_text("x")
        with simulated_clone() as spy, self.assertRaises(OSError):
            _secure_copy_file(directory, self.dst, shared_root=self.shared)
        # A non-regular source never reaches clonefile(2).
        spy.assert_not_called()
        self.assertFalse(sync_module._lexists(self.dst))
        self.assertEqual(os.listdir(self.dst.parent), [])

    def test_directory_clone_is_removed_and_refused(self) -> None:
        def clone_a_directory(src, dst):
            # The source was swapped for a directory after the regular-file
            # check: clonefile(2) would clone the whole tree.
            Path(dst).mkdir()
            (Path(dst) / "nested").mkdir()
            (Path(dst) / "nested" / "file").write_text("x")
            (Path(dst) / "nested").chmod(0o500)
            Path(dst).chmod(0o500)

        with (
            simulated_clone(clone_a_directory),
            forbid_byte_copy(),
            self.assertRaisesRegex(StorageSafetyError, "directory clone"),
        ):
            self._copy()
        self.assertFalse(sync_module._lexists(self.dst))
        self.assertEqual(os.listdir(self.dst.parent), [])

    @needs_darwin_clone
    def test_darwin_real_directory_clone_is_removed_and_refused(self) -> None:
        swapped = self.root / "src" / "swapped"

        def swap_then_clone(src, dst):
            os.replace(src, swapped)
            Path(src).mkdir()
            (Path(src) / "inner").write_text("x")
            Path(src).chmod(0o500)
            REAL_CLONEFILE(src, dst)

        try:
            with (
                mock.patch.object(apfs, "clonefile", side_effect=swap_then_clone),
                forbid_byte_copy(),
                self.assertRaisesRegex(StorageSafetyError, "directory clone"),
            ):
                self._copy()
        finally:
            self.src.chmod(0o700)
        self.assertFalse(sync_module._lexists(self.dst))
        self.assertEqual(os.listdir(self.dst.parent), [])

    def test_group_accessible_staging_directory_skips_clone(self) -> None:
        loose = self.root / "loose"
        loose.mkdir()
        loose.chmod(0o755)
        with simulated_clone() as spy:
            self.assertIsNone(_clone_to_temporary_sibling(self.src, loose / "x"))
        spy.assert_not_called()

    def test_clone_with_extended_acl_falls_back_to_byte_copy(self) -> None:
        with (
            simulated_clone() as spy,
            mock.patch.object(apfs, "has_extended_acl", return_value=True),
            mock.patch.object(
                sync_module.shutil,
                "copyfileobj",
                wraps=sync_module.shutil.copyfileobj,
            ) as byte_copy,
        ):
            self._copy()
        spy.assert_called_once()
        byte_copy.assert_called_once()
        self.assertEqual(self.dst.read_bytes(), self.payload)
        self.assertEqual(self.dst.stat().st_mode & 0o777, 0o600)
        self.assertEqual(staging_leftovers(self.dst.parent), [])

    def test_acl_probe_failure_fails_closed(self) -> None:
        self.dst.parent.mkdir(parents=True)
        self.dst.write_bytes(b"previous complete copy\n")
        with (
            simulated_clone(),
            forbid_byte_copy(),
            mock.patch.object(
                apfs, "has_extended_acl", side_effect=OSError(errno.EIO, "acl")
            ),
            self.assertRaises(OSError),
        ):
            self._copy()
        self.assertEqual(self.dst.read_bytes(), b"previous complete copy\n")
        self.assertEqual(staging_leftovers(self.dst.parent), [])

    @needs_darwin_clone
    def test_darwin_clone_does_not_carry_source_acl(self) -> None:
        granted = subprocess.run(
            ["chmod", "+a", "everyone allow read", str(self.src)],
            check=False,
            capture_output=True,
            text=True,
        )
        if granted.returncode != 0:
            self.skipTest(f"cannot set an ACL here: {granted.stderr.strip()}")
        fd = os.open(self.src, os.O_RDONLY)
        try:
            self.assertTrue(apfs.has_extended_acl(fd))
        finally:
            os.close(fd)

        self._copy()

        fd = os.open(self.dst, os.O_RDONLY)
        try:
            self.assertFalse(apfs.has_extended_acl(fd))
        finally:
            os.close(fd)
        self.assertEqual(self.dst.stat().st_mode & 0o777, 0o600)


class ClonefileWrapperTests(unittest.TestCase):
    """apfs.clonefile maps errno the same way on every platform."""

    @staticmethod
    def _failing(*errors: int):
        calls: list[tuple[bytes, bytes, int]] = []
        remaining = list(errors)

        def function(src, dst, flags):
            calls.append((src, dst, flags))
            if not remaining:
                return 0
            ctypes.set_errno(remaining.pop(0))
            return -1

        return function, calls

    def test_unsupported_errnos_raise_clone_unsupported(self) -> None:
        for error in sorted(apfs.CLONE_UNSUPPORTED_ERRNOS):
            function, calls = self._failing(error)
            with (
                self.subTest(errno=errno.errorcode.get(error, error)),
                mock.patch.object(apfs, "_clonefile_function", return_value=function),
                self.assertRaises(apfs.CloneUnsupported) as raised,
            ):
                apfs.clonefile("a", "b")
            self.assertEqual(raised.exception.errno, error)
            self.assertEqual(calls, [(b"a", b"b", apfs.CLONE_NOOWNERCOPY)])

    def test_existing_destination_is_file_exists(self) -> None:
        function, _ = self._failing(errno.EEXIST)
        with (
            mock.patch.object(apfs, "_clonefile_function", return_value=function),
            self.assertRaises(FileExistsError),
        ):
            apfs.clonefile("a", "b")

    def test_other_errors_are_os_errors_not_fallbacks(self) -> None:
        for error in (errno.EIO, errno.ENOSPC, errno.EACCES, errno.EPERM):
            function, _ = self._failing(error)
            with (
                self.subTest(errno=errno.errorcode[error]),
                mock.patch.object(apfs, "_clonefile_function", return_value=function),
                self.assertRaises(OSError) as raised,
            ):
                apfs.clonefile("a", "b")
            self.assertNotIsInstance(raised.exception, apfs.CloneUnsupported)
            self.assertEqual(raised.exception.errno, error)

    def test_eintr_is_retried(self) -> None:
        function, calls = self._failing(errno.EINTR)
        with mock.patch.object(apfs, "_clonefile_function", return_value=function):
            apfs.clonefile("a", "b")
        self.assertEqual(len(calls), 2)

    def test_acl_probe_maps_errno_and_frees(self) -> None:
        freed: list[int] = []

        def probe(result: int | None, error: int = 0) -> bool:
            def get_fd(fd, acl_type):
                ctypes.set_errno(error)
                return result

            with mock.patch.object(
                apfs, "_acl_functions", return_value=(get_fd, freed.append)
            ):
                return apfs.has_extended_acl(0)

        self.assertTrue(probe(1234))
        self.assertEqual(freed, [1234])
        for error in (errno.ENOENT, errno.ENOTSUP, errno.EOPNOTSUPP):
            with self.subTest(errno=errno.errorcode[error]):
                self.assertFalse(probe(None, error))
        with self.assertRaises(OSError) as raised:
            probe(None, errno.EIO)
        self.assertEqual(raised.exception.errno, errno.EIO)

    @unittest.skipIf(sys.platform == "darwin", "exercises the non-macOS stubs")
    def test_primitives_report_unsupported_off_macos(self) -> None:
        self.assertFalse(apfs.clone_available())
        with self.assertRaises(apfs.CloneUnsupported):
            apfs.clonefile("a", "b")
        self.assertIsNone(apfs.private_size(__file__))
        fd = os.open(__file__, os.O_RDONLY)
        try:
            self.assertFalse(apfs.has_extended_acl(fd))
        finally:
            os.close(fd)

    @needs_darwin_clone
    def test_darwin_private_size_tracks_block_sharing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "original"
            original.write_bytes(os.urandom(512 * 1024))
            clone = Path(tmp) / "clone"
            apfs.clonefile(original, clone)
            self.assertEqual(apfs.private_size(clone), 0)
            with original.open("ab") as handle:
                handle.write(os.urandom(64 * 1024))
                handle.flush()
                os.fsync(handle.fileno())
            self.assertGreater(apfs.private_size(original), 0)
            self.assertEqual(clone.stat().st_size, 512 * 1024)
            with self.assertRaises(FileExistsError):
                apfs.clonefile(original, clone)


class SyncLockHelperTests(unittest.TestCase):
    def test_lock_is_exclusive_and_released(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "logpile.db"
            with sync_lock(db_path) as outer:
                self.assertTrue(outer)
                with sync_lock(db_path) as inner:
                    self.assertFalse(inner)
            with sync_lock(db_path) as again:
                self.assertTrue(again)
            lock_path = Path(f"{db_path}.sync.lock")
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RecloneSharedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home" / ".claude" / "projects" / "demo"
        self.home.mkdir(parents=True)
        self.shared = self.root / "shared"
        self.private_root = self.root / ".shared-private"
        self.db_path = self.root / "logpile.db"
        init_db(self.db_path)
        self._rows = 0

    def _add_row(self, source: Path, shared: Path) -> None:
        self._rows += 1
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO sessions (session_id, source, username, source_path, shared_path)
                VALUES (?, 'claudecode', 'alice', ?, ?)
                """,
                (f"session-{self._rows}", str(source), str(shared)),
            )
            conn.commit()
        finally:
            conn.close()

    def _pair(
        self,
        name: str,
        *,
        content: bytes | None = None,
        shared_content: bytes | None = None,
        root: Path | None = None,
    ) -> tuple[Path, Path]:
        content = content if content is not None else os.urandom(64 * 1024)
        source = self.home / f"{name}.jsonl"
        source.write_bytes(content)
        source.chmod(0o644)
        shared = (root or self.shared) / "alice" / "claudecode" / "demo" / source.name
        shared.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shared.write_bytes(content if shared_content is None else shared_content)
        shared.chmod(0o600)
        self._add_row(source, shared)
        return source, shared

    def _snapshot(self, *paths: Path) -> dict:
        state = {}
        for path in paths:
            info = os.lstat(path)
            state[path] = (
                info.st_ino,
                info.st_mode,
                os.readlink(path) if os.path.islink(path) else sha256_of(path),
            )
        return state

    def _db_digest(self) -> str:
        return sha256_of(self.db_path)

    def _run(self, *, apply: bool):
        return reclone_shared_copies(self.db_path, self.shared, apply=apply)

    def _leftovers(self) -> list[Path]:
        return [
            path
            for base in (self.shared, self.private_root, self.home)
            if base.exists()
            for path in base.rglob("*.tmp-sync")
        ]

    def test_dry_run_reports_without_changing_anything(self) -> None:
        source, shared = self._pair("identical")
        before = self._snapshot(source, shared)
        db_before = self._db_digest()

        with simulated_clone() as spy:
            report = self._run(apply=False)

        spy.assert_not_called()
        self.assertFalse(report.applied)
        self.assertEqual(report.examined, 1)
        self.assertEqual(report.identical, 1)
        self.assertEqual(report.recloned, 1)
        self.assertEqual(report.recloned_bytes, shared.stat().st_size)
        self.assertGreater(report.reclaim_bytes, 0)
        self.assertEqual(report.skipped_total, 0)
        self.assertIsNotNone(report.free_before)
        self.assertIsNotNone(report.free_after)
        self.assertEqual(self._snapshot(source, shared), before)
        self.assertEqual(self._db_digest(), db_before)
        self.assertEqual(self._leftovers(), [])

    def test_apply_replaces_identical_copy_with_verified_clone(self) -> None:
        source, shared = self._pair("identical")
        content = shared.read_bytes()
        source_before = self._snapshot(source)
        shared_inode = shared.stat().st_ino
        db_before = self._db_digest()

        with simulated_clone() as spy:
            report = self._run(apply=True)

        spy.assert_called_once()
        self.assertEqual(Path(spy.call_args.args[0]), source)
        self.assertEqual(Path(spy.call_args.args[1]).parent, shared.parent)
        self.assertTrue(report.applied)
        self.assertEqual((report.identical, report.recloned), (1, 1))
        self.assertNotEqual(shared.stat().st_ino, shared_inode)
        self.assertEqual(shared.read_bytes(), content)
        self.assertEqual(shared.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self._snapshot(source), source_before)
        self.assertEqual(self._db_digest(), db_before)
        self.assertEqual(self._leftovers(), [])

    def test_skips_mismatches_missing_sources_and_symlinked_copies(self) -> None:
        _, identical = self._pair("identical")
        size_src, size_shared = self._pair(
            "size", content=b"source grew\n", shared_content=b"older\n"
        )
        hash_src, hash_shared = self._pair(
            "hash", content=b"version A\n", shared_content=b"version B\n"
        )
        gone_src, gone_shared = self._pair("gone")
        gone_src.unlink()

        link_target = self.root / "outside-target.jsonl"
        link_src = self.home / "link.jsonl"
        link_src.write_bytes(b"same bytes\n")
        link_target.write_bytes(b"same bytes\n")
        link_shared = self.shared / "alice" / "claudecode" / "demo" / "link.jsonl"
        link_shared.symlink_to(link_target)
        self._add_row(link_src, link_shared)

        untouched = [
            size_src,
            size_shared,
            hash_src,
            hash_shared,
            gone_shared,
            link_src,
            link_shared,
            link_target,
        ]
        before = self._snapshot(*untouched)

        with simulated_clone():
            report = self._run(apply=True)

        self.assertEqual(report.examined, 5)
        self.assertEqual(report.recloned, 1)
        self.assertEqual(report.examined, report.recloned + report.skipped_total)
        self.assertEqual(
            dict(report.skipped),
            {
                reclone_module.SIZE_MISMATCH: 1,
                reclone_module.HASH_MISMATCH: 1,
                reclone_module.SOURCE_MISSING: 1,
                reclone_module.SHARED_NOT_REGULAR: 1,
            },
        )
        self.assertEqual(
            report.skipped_bytes[reclone_module.SOURCE_MISSING],
            gone_shared.stat().st_size,
        )
        self.assertEqual(self._snapshot(*untouched), before)
        self.assertTrue(link_shared.is_symlink())
        self.assertEqual(identical.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self._leftovers(), [])

    def test_private_archive_rows_pass_the_same_checks(self) -> None:
        _, archived = self._pair("private", root=self.private_root)
        archived_inode = archived.stat().st_ino
        _, stale = self._pair(
            "private-stale",
            content=b"new\n",
            shared_content=b"old\n",
            root=self.private_root,
        )
        stale_before = self._snapshot(stale)

        with simulated_clone():
            report = self._run(apply=True)

        self.assertEqual(report.examined_by_root["private"], 2)
        self.assertEqual(report.recloned, 1)
        self.assertNotEqual(archived.stat().st_ino, archived_inode)
        self.assertEqual(archived.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self._snapshot(stale), stale_before)

    def test_rows_outside_managed_roots_are_never_touched(self) -> None:
        _, elsewhere = self._pair("elsewhere", root=self.root / "elsewhere")
        before = self._snapshot(elsewhere)

        with simulated_clone() as spy:
            report = self._run(apply=True)

        spy.assert_not_called()
        self.assertEqual(report.skipped[reclone_module.OUTSIDE_MANAGED_ROOT], 1)
        self.assertEqual(self._snapshot(elsewhere), before)

    def test_symlinked_directory_inside_root_is_not_followed(self) -> None:
        real = self.root / "real-dir"
        real.mkdir(mode=0o700)
        source = self.home / "via-link.jsonl"
        source.write_bytes(b"payload\n")
        (real / "via-link.jsonl").write_bytes(b"payload\n")
        self.shared.mkdir(mode=0o700)
        (self.shared / "alice").symlink_to(real)
        shared = self.shared / "alice" / "via-link.jsonl"
        self._add_row(source, shared)
        before = self._snapshot(real / "via-link.jsonl")

        with simulated_clone() as spy:
            report = self._run(apply=True)

        spy.assert_not_called()
        self.assertEqual(report.skipped[reclone_module.UNSAFE_ANCESTRY], 1)
        self.assertEqual(self._snapshot(real / "via-link.jsonl"), before)

    def test_refuses_when_sync_lock_is_held(self) -> None:
        source, shared = self._pair("identical")
        before = self._snapshot(source, shared)
        lock_path = Path(f"{self.db_path}.sync.lock")
        with open(lock_path, "w") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with simulated_clone() as spy:
                with self.assertRaises(RecloneLockContended):
                    self._run(apply=True)
                result = CliRunner().invoke(
                    cli,
                    [
                        "reclone-shared",
                        "--apply",
                        "--db",
                        str(self.db_path),
                        "--shared-dir",
                        str(self.shared),
                    ],
                )
            spy.assert_not_called()
        self.assertEqual(result.exit_code, 75, result.output)
        self.assertIn("holds", result.output)
        self.assertIn("nothing was examined or changed", result.output)
        self.assertEqual(self._snapshot(source, shared), before)

    def test_source_change_after_hashing_is_never_installed(self) -> None:
        _, shared = self._pair("racing")
        before = self._snapshot(shared)

        def clone_newer_source(src, dst):
            with open(src, "ab") as handle:
                handle.write(b"appended after hashing\n")
            fake_clonefile(src, dst)

        with simulated_clone(clone_newer_source):
            report = self._run(apply=True)

        self.assertEqual(report.recloned, 0)
        self.assertEqual(report.skipped[reclone_module.CLONE_CHANGED], 1)
        self.assertEqual(self._snapshot(shared), before)
        self.assertEqual(self._leftovers(), [])

    def test_shared_copy_changed_during_run_is_not_overwritten(self) -> None:
        _, shared = self._pair("replaced")
        content = shared.read_bytes()

        rewritten: list[int] = []

        def clone_while_sync_rewrites(src, dst):
            fake_clonefile(src, dst)
            replacement = shared.with_name("replacement")
            replacement.write_bytes(content)
            os.replace(replacement, shared)
            rewritten.append(shared.stat().st_ino)

        with simulated_clone(clone_while_sync_rewrites):
            report = self._run(apply=True)

        self.assertEqual(report.recloned, 0)
        self.assertEqual(report.skipped[reclone_module.SHARED_CHANGED], 1)
        self.assertEqual([shared.stat().st_ino], rewritten)
        self.assertEqual(self._leftovers(), [])

    def test_interrupt_leaves_old_copy_and_releases_lock(self) -> None:
        source, shared = self._pair("interrupted")
        before = self._snapshot(source, shared)
        real_hash = reclone_module.file_hash

        def interrupt_on_clone(path):
            if str(path).endswith(".tmp-sync"):
                raise KeyboardInterrupt
            return real_hash(path)

        with (
            simulated_clone(),
            mock.patch.object(
                reclone_module, "file_hash", side_effect=interrupt_on_clone
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._run(apply=True)

        self.assertEqual(self._snapshot(source, shared), before)
        self.assertEqual(self._leftovers(), [])
        with sync_lock(self.db_path) as acquired:
            self.assertTrue(acquired)

    def test_read_errors_are_reported_and_exit_nonzero(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        source, shared = self._pair("unreadable")
        source.chmod(0o000)
        self.addCleanup(source.chmod, 0o600)
        before = self._snapshot(shared)

        with simulated_clone():
            result = CliRunner().invoke(
                cli,
                [
                    "reclone-shared",
                    "--apply",
                    "--db",
                    str(self.db_path),
                    "--shared-dir",
                    str(self.shared),
                ],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("error", result.output)
        self.assertEqual(self._snapshot(shared), before)

    def test_cli_dry_run_then_apply(self) -> None:
        _, shared = self._pair("identical")
        self._pair("gone")[0].unlink()
        args = ["--db", str(self.db_path), "--shared-dir", str(self.shared)]

        with simulated_clone():
            dry = CliRunner().invoke(cli, ["reclone-shared", *args])
            applied = CliRunner().invoke(cli, ["reclone-shared", "--apply", *args])

        self.assertEqual(dry.exit_code, 0, dry.output)
        self.assertIn("DRY RUN", dry.output)
        self.assertIn("Examined: 2 shared copies", dry.output)
        self.assertIn("Identical to source: 1", dry.output)
        self.assertIn("Would reclone: 1 file,", dry.output)
        self.assertIn("source_missing", dry.output)
        self.assertIn("Free space before:", dry.output)
        self.assertIn("Free space after:", dry.output)
        self.assertEqual(applied.exit_code, 0, applied.output)
        self.assertIn("Recloned: 1 file,", applied.output)
        self.assertEqual(shared.stat().st_mode & 0o777, 0o600)

    @needs_darwin_clone
    def test_darwin_apply_shares_blocks_and_rerun_skips(self) -> None:
        source, shared = self._pair("identical", content=os.urandom(512 * 1024))
        self.assertGreater(apfs.private_size(shared), 0)

        report = self._run(apply=True)

        self.assertEqual(report.recloned, 1)
        self.assertGreater(report.reclaim_bytes, 0)
        self.assertEqual(apfs.private_size(shared), 0)
        self.assertEqual(shared.read_bytes(), source.read_bytes())
        self.assertEqual(shared.stat().st_mode & 0o777, 0o600)

        rerun = self._run(apply=False)
        self.assertEqual(rerun.identical, 0)
        self.assertEqual(rerun.skipped[reclone_module.ALREADY_SHARED], 1)

        # The clone stays independent of its source.
        with source.open("ab") as handle:
            handle.write(b"later\n")
        source_bytes = source.read_bytes()
        self.assertNotEqual(shared.read_bytes(), source_bytes)
        source.unlink()
        self.assertEqual(len(shared.read_bytes()), 512 * 1024)


if __name__ == "__main__":
    unittest.main()
