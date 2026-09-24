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
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from click.testing import CliRunner

from logpile import apfs
from logpile import reclone as reclone_module
from logpile import sync as sync_module
from logpile.cli import cli
from logpile.db import get_db, init_db, set_session_visibility
from logpile.reclone import RecloneLockContended, reclone_shared_copies
from logpile.sync import (
    StorageSafetyError,
    _clone_allowed_by_flags,
    _clone_to_temporary_sibling,
    _discard_temporary,
    _secure_copy_file,
    sync_lock,
)

REAL_CLONEFILE = apfs.clonefile
REAL_RENAME_SWAP = apfs.rename_swap
REAL_FILE_STORAGE = apfs.file_storage
DARWIN_CLONE = sys.platform == "darwin" and apfs.clone_available()
needs_darwin_clone = unittest.skipUnless(
    DARWIN_CLONE, "real clonefile(2) requires macOS on APFS"
)
# <sys/fcntl.h>: F_LOG2PHYS_EXT maps a file range to its device offset.
F_LOG2PHYS_EXT = 65
LOG2PHYS = struct.Struct("=Iqq")  # struct log2phys under #pragma pack(4)


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


def fake_rename_swap(first, second) -> None:
    """Stand-in for renamex_np(RENAME_SWAP): ENOENT unless both paths exist."""
    for path in (first, second):
        if not os.path.lexists(path):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
    parking = f"{second}.swap-parking"
    os.rename(first, parking)
    os.rename(second, first)
    os.rename(parking, second)


def portable_rename_swap(first, second) -> None:
    """The real swap on macOS, the stand-in where the platform has none."""
    try:
        REAL_RENAME_SWAP(first, second)
    except apfs.SwapUnsupported:
        fake_rename_swap(first, second)


@contextmanager
def simulated_clone(side_effect=fake_clonefile, swap=portable_rename_swap):
    """Force the clone path on any platform; yields the clonefile spy."""
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(apfs, "clone_available", return_value=True)
        )
        spy = stack.enter_context(
            mock.patch.object(apfs, "clonefile", side_effect=side_effect)
        )
        stack.enter_context(mock.patch.object(apfs, "rename_swap", side_effect=swap))
        yield spy


def storage_overrides(overrides: dict):
    """Patch apfs.file_storage: ``overrides`` maps a path to a FileStorage
    (or a callable taking the real result); other paths get the real answer."""

    def file_storage(path):
        real = REAL_FILE_STORAGE(path)
        override = overrides.get(Path(path))
        if override is None:
            return real
        return override(real) if callable(override) else override

    return mock.patch.object(apfs, "file_storage", side_effect=file_storage)


def physical_extents(path: Path) -> list[tuple[int, int, int]]:
    """(file offset, device offset, length) runs, via F_LOG2PHYS_EXT."""
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        offset = 0
        runs = []
        while offset < size:
            reply = fcntl.fcntl(
                fd, F_LOG2PHYS_EXT, LOG2PHYS.pack(0, size - offset, offset)
            )
            _, contiguous, device = LOG2PHYS.unpack(reply)
            if contiguous <= 0:
                raise AssertionError(f"F_LOG2PHYS_EXT made no progress at {offset}")
            runs.append((offset, device, contiguous))
            offset += contiguous
        return runs
    finally:
        os.close(fd)


def clear_flags(*paths: Path) -> None:
    """Test cleanup: drop BSD flags so TemporaryDirectory can delete files."""
    for path in paths:
        try:
            os.lchflags(path, 0)
        except (AttributeError, OSError):
            pass


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
        # A clone owns no private blocks until one side is written, and it is
        # the same APFS data stream as its source.
        self.assertEqual(apfs.private_size(self.dst), 0)
        self.assertTrue(
            apfs.file_storage(self.dst).is_clone_of(apfs.file_storage(self.src))
        )
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

    def test_source_flags_that_block_cloning(self) -> None:
        def flags(value: int) -> SimpleNamespace:
            return SimpleNamespace(st_flags=value)

        self.assertTrue(_clone_allowed_by_flags(SimpleNamespace()))  # Linux
        for allowed in (
            0,
            stat.UF_NODUMP | stat.UF_HIDDEN,
            stat.UF_COMPRESSED,
            0x40,  # UF_TRACKED
        ):
            self.assertTrue(_clone_allowed_by_flags(flags(allowed)), hex(allowed))
        for blocked in (
            stat.UF_IMMUTABLE,
            stat.UF_APPEND,
            0x80,  # UF_DATAVAULT
            stat.SF_ARCHIVED,
            stat.SF_IMMUTABLE,
            stat.SF_APPEND,
            stat.SF_NOUNLINK,
            0x40000000,  # SF_DATALESS
        ):
            self.assertFalse(_clone_allowed_by_flags(flags(blocked)), hex(blocked))

    def test_clone_with_superuser_flag_falls_back_to_byte_copy(self) -> None:
        # Only root can set or clear SF_* flags, so simulate a clone that
        # came back with one (root set it on the source mid-copy).
        real_fstat = os.fstat

        def fstat_with_system_flag(fd):
            result = real_fstat(fd)
            fields = {
                name: getattr(result, name)
                for name in dir(result)
                if name.startswith("st_")
            }
            fields["st_flags"] = stat.SF_IMMUTABLE
            return SimpleNamespace(**fields)

        with (
            simulated_clone() as spy,
            mock.patch.object(
                sync_module.os, "fstat", side_effect=fstat_with_system_flag
            ),
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

    @needs_darwin_clone
    def test_darwin_locked_source_falls_back_to_byte_copy(self) -> None:
        # clonefile(2) copies BSD flags: a uchg/uappnd clone would refuse the
        # fchmod and the cleanup unlink, leaving undeletable debris.
        for flag in (stat.UF_IMMUTABLE, stat.UF_APPEND):
            with self.subTest(flag=hex(flag)):
                self.src.chmod(0o644)
                os.chflags(self.src, flag)
                self.addCleanup(clear_flags, self.src)
                try:
                    for _ in range(3):
                        with (
                            mock.patch.object(
                                apfs, "clonefile", wraps=REAL_CLONEFILE
                            ) as clone,
                            mock.patch.object(
                                sync_module.shutil,
                                "copyfileobj",
                                wraps=sync_module.shutil.copyfileobj,
                            ) as byte_copy,
                        ):
                            self._copy()
                        clone.assert_not_called()
                        byte_copy.assert_called_once()
                        info = self.dst.lstat()
                        self.assertEqual(info.st_mode & 0o777, 0o600)
                        self.assertEqual(info.st_flags, 0)
                        self.assertEqual(self.dst.read_bytes(), self.payload)
                        self.assertEqual(staging_leftovers(self.dst.parent), [])
                finally:
                    clear_flags(self.src)

    @needs_darwin_clone
    def test_darwin_cosmetic_source_flags_are_cleared_on_the_clone(self) -> None:
        os.chflags(self.src, stat.UF_HIDDEN | stat.UF_NODUMP)
        self.addCleanup(clear_flags, self.src)
        with forbid_byte_copy():
            self._copy()
        info = self.dst.lstat()
        self.assertEqual(info.st_flags, 0)
        self.assertEqual(info.st_mode & 0o777, 0o600)
        self.assertEqual(self.dst.read_bytes(), self.payload)
        self.assertEqual(apfs.private_size(self.dst), 0)

    @needs_darwin_clone
    def test_darwin_compressed_source_clone_keeps_its_data(self) -> None:
        # A decmpfs file keeps its bytes in an xattr; clearing UF_COMPRESSED
        # on its clone would leave an empty file.
        text = "".join(
            f'{{"line": {i}, "text": "hello {i % 97}"}}\n' for i in range(40000)
        )
        plain = self.root / "src" / "plain.jsonl"
        plain.write_text(text)
        compressed = self.root / "src" / "compressed.jsonl"
        made = subprocess.run(
            ["ditto", "--hfsCompression", str(plain), str(compressed)],
            check=False,
            capture_output=True,
        )
        if made.returncode != 0 or not (
            compressed.lstat().st_flags & stat.UF_COMPRESSED
        ):
            self.skipTest("ditto could not make an HFS-compressed file here")
        # A cosmetic flag too, so the clear-all-but-compressed mask does work.
        os.chflags(compressed, stat.UF_COMPRESSED | stat.UF_HIDDEN)
        self.addCleanup(clear_flags, compressed)
        with forbid_byte_copy():
            _secure_copy_file(compressed, self.dst, shared_root=self.shared)
        info = self.dst.lstat()
        self.assertEqual(info.st_flags, stat.UF_COMPRESSED)
        self.assertEqual(info.st_mode & 0o777, 0o600)
        self.assertEqual(info.st_size, len(text.encode()))
        self.assertEqual(self.dst.read_text(), text)

    @needs_darwin_clone
    def test_darwin_lock_flag_set_on_the_clone_is_cleared(self) -> None:
        # The source is flag-free when checked, but the clone arrives locked
        # (the source was locked in between): the flags are cleared, not fatal.
        def clone_then_lock(src, dst):
            REAL_CLONEFILE(src, dst)
            os.chflags(dst, stat.UF_IMMUTABLE | stat.UF_APPEND)

        with (
            mock.patch.object(apfs, "clonefile", side_effect=clone_then_lock),
            # Flags change through the verified descriptor, never the path.
            mock.patch.object(
                sync_module.os, "lchflags", side_effect=AssertionError("by path")
            ),
            mock.patch.object(
                apfs, "set_file_flags", wraps=apfs.set_file_flags
            ) as set_flags,
            forbid_byte_copy(),
        ):
            self._copy()
        set_flags.assert_called_once()
        info = self.dst.lstat()
        self.assertEqual(info.st_flags, 0)
        self.assertEqual(info.st_mode & 0o777, 0o600)
        self.assertEqual(staging_leftovers(self.dst.parent), [])

    @needs_darwin_clone
    def test_darwin_discard_removes_a_locked_staging_file(self) -> None:
        staged = self.root / ".session.jsonl.0123456789abcdef.tmp-sync"
        staged.write_bytes(b"x")
        os.chflags(staged, stat.UF_IMMUTABLE | stat.UF_APPEND)
        self.addCleanup(clear_flags, staged)
        _discard_temporary(staged)
        self.assertFalse(sync_module._lexists(staged))


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

    def test_clonefile_flags_argument_is_uint32(self) -> None:
        # <sys/clonefile.h>: int clonefile(const char *, const char *, uint32_t)
        function = apfs._clonefile_function()
        if function is None:
            self.skipTest("no clonefile(2) here")
        self.assertEqual(function.argtypes[2], ctypes.c_uint32)

    def test_rename_swap_maps_errno(self) -> None:
        def run(*errors: int):
            function, calls = self._failing(*errors)
            with mock.patch.object(apfs, "_renamex_function", return_value=function):
                apfs.rename_swap("a", "b")
            return calls

        self.assertEqual(run(), [(b"a", b"b", apfs.RENAME_SWAP)])
        self.assertEqual(len(run(errno.EINTR)), 2)
        for error in sorted(apfs.SWAP_UNSUPPORTED_ERRNOS):
            with (
                self.subTest(errno=errno.errorcode.get(error, error)),
                self.assertRaises(apfs.SwapUnsupported) as raised,
            ):
                run(error)
            self.assertEqual(raised.exception.errno, error)
        with self.assertRaises(FileNotFoundError):
            run(errno.ENOENT)
        # EINVAL means a bad flags value: a bug, never a silent fallback.
        for error in (errno.EINVAL, errno.EIO, errno.EPERM):
            with (
                self.subTest(errno=errno.errorcode[error]),
                self.assertRaises(OSError) as raised,
            ):
                run(error)
            self.assertNotIsInstance(raised.exception, apfs.SwapUnsupported)
            self.assertEqual(raised.exception.errno, error)

    def test_file_storage_reply_parsing(self) -> None:
        returned_all = 0x8 | 0x80 | 0x100 | 0x200
        body = (
            struct.pack("=q", 4096)
            + struct.pack("=2i", 16777229, 26)
            + struct.pack("=Q", 1234)
            + struct.pack("=Q", apfs.EF_SHARES_ALL_BLOCKS | 0x3)
        )

        def reply(returned: int, payload: bytes, length: int | None = None) -> bytes:
            header_size = struct.calcsize("=I5I")
            size = header_size + len(payload) if length is None else length
            return struct.pack("=I5I", size, 0x80000000, 0, 0, 0, returned) + payload

        self.assertEqual(
            apfs._parse_file_storage(reply(returned_all, body)),
            apfs.FileStorage(
                fsid=(16777229, 26),
                clone_id=1234,
                private_size=4096,
                ext_flags=apfs.EF_SHARES_ALL_BLOCKS | 0x3,
            ),
        )
        # Only what the volume returned is decoded; the rest stays unknown.
        self.assertEqual(
            apfs._parse_file_storage(reply(0x100, struct.pack("=Q", 99))),
            apfs.FileStorage(clone_id=99),
        )
        # A reply shorter than its bitmap promises is rejected, not misread.
        self.assertIsNone(apfs._parse_file_storage(reply(returned_all, body[:12])))
        self.assertIsNone(apfs._parse_file_storage(b"\x00" * 8))
        negative = apfs._parse_file_storage(reply(0x8, struct.pack("=q", -1)))
        self.assertIsNone(negative.private_size)

    def test_clone_match_needs_same_volume_id_and_shared_blocks(self) -> None:
        full = apfs.EF_SHARES_ALL_BLOCKS | 0x1
        clone = apfs.FileStorage(
            fsid=(1, 26), clone_id=7, private_size=0, ext_flags=full
        )
        self.assertTrue(clone.is_clone_of(replace(clone)))
        for other in (
            replace(clone, clone_id=8),  # a byte copy: its own data stream
            replace(clone, fsid=(2, 26)),  # ids are per volume
            replace(clone, clone_id=None),
            replace(clone, fsid=None),
            replace(clone, ext_flags=0x3),  # the source must share too
            replace(clone, ext_flags=None),
            apfs.FileStorage(),
        ):
            with self.subTest(other=other):
                self.assertFalse(clone.is_clone_of(other))
        for mine in (
            replace(clone, ext_flags=0x3),  # no EF_SHARES_ALL_BLOCKS
            replace(clone, ext_flags=None),
            replace(clone, clone_id=0),
            apfs.FileStorage(),
        ):
            with self.subTest(mine=mine):
                self.assertFalse(mine.is_clone_of(replace(mine)))
        # Private size plays no part: a snapshot can zero a byte copy's.
        copy = replace(clone, clone_id=8, private_size=0, ext_flags=0x2)
        self.assertFalse(copy.is_clone_of(clone))

    @unittest.skipIf(sys.platform == "darwin", "exercises the non-macOS stubs")
    def test_primitives_report_unsupported_off_macos(self) -> None:
        self.assertFalse(apfs.clone_available())
        with self.assertRaises(apfs.CloneUnsupported):
            apfs.clonefile("a", "b")
        with self.assertRaises(apfs.SwapUnsupported):
            apfs.rename_swap("a", "b")
        self.assertIsNone(apfs.file_storage(__file__))
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

    @needs_darwin_clone
    def test_darwin_clone_id_agrees_with_physical_extents(self) -> None:
        """Clone ids say "shares every block" exactly when F_LOG2PHYS_EXT
        maps both files onto the same device extents."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.write_bytes(os.urandom(3 * 1024 * 1024 + 123))
            with source.open("rb+") as handle:
                os.fsync(handle.fileno())
            byte_copy = Path(tmp) / "byte-copy"
            byte_copy.write_bytes(source.read_bytes())
            with byte_copy.open("rb+") as handle:
                os.fsync(handle.fileno())
            clone = Path(tmp) / "clone"
            apfs.clonefile(source, clone)

            def storage(path):
                return apfs.file_storage(path)

            self.assertTrue(storage(clone).is_clone_of(storage(source)))
            self.assertEqual(physical_extents(clone), physical_extents(source))
            self.assertFalse(storage(byte_copy).is_clone_of(storage(source)))
            self.assertNotEqual(physical_extents(byte_copy), physical_extents(source))

            # Writing either side gives it its own data stream again.
            with source.open("ab") as handle:
                handle.write(b"appended\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.assertFalse(storage(clone).is_clone_of(storage(source)))
            self.assertNotEqual(physical_extents(clone), physical_extents(source))

    @needs_darwin_clone
    def test_darwin_rename_swap_exchanges_and_never_creates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "first", Path(tmp) / "second"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            inodes = (first.stat().st_ino, second.stat().st_ino)
            apfs.rename_swap(first, second)
            self.assertEqual(
                (first.read_bytes(), second.read_bytes()), (b"two", b"one")
            )
            self.assertEqual((second.stat().st_ino, first.stat().st_ino), inodes)
            second.unlink()
            with self.assertRaises(FileNotFoundError):
                apfs.rename_swap(first, second)
            self.assertFalse(second.exists())
            self.assertEqual(first.read_bytes(), b"two")

    @unittest.skipUnless(
        DARWIN_CLONE and Path("/usr/share/dict/words").is_file(),
        "needs the macOS sealed system volume",
    )
    def test_darwin_real_volume_id_tells_system_from_data_volume(self) -> None:
        system = apfs.file_storage("/usr/share/dict/words")
        with tempfile.NamedTemporaryFile() as handle:
            data = apfs.file_storage(handle.name)
        self.assertIsNotNone(system.fsid)
        self.assertIsNotNone(data.fsid)
        self.assertNotEqual(system.fsid, data.fsid)


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


class _RecloneFixture(unittest.TestCase):
    """A logpile DB with source/shared pairs; no tests of its own."""

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


class RecloneSharedTests(_RecloneFixture):
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
        self.assertGreater(report.reclaim_eventual_bytes, 0)
        self.assertLessEqual(report.reclaim_now_bytes, report.reclaim_eventual_bytes)
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
        self.assertFalse(
            apfs.file_storage(shared).is_clone_of(apfs.file_storage(source))
        )

        report = self._run(apply=True)

        self.assertEqual(report.recloned, 1)
        self.assertEqual(report.reclaim_eventual_bytes, 512 * 1024)
        self.assertEqual(report.errors, [])
        self.assertEqual(apfs.private_size(shared), 0)
        self.assertTrue(
            apfs.file_storage(shared).is_clone_of(apfs.file_storage(source))
        )
        self.assertEqual(shared.read_bytes(), source.read_bytes())
        self.assertEqual(shared.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self._leftovers(), [])

        # A re-run recognises the clone without hashing it.
        with mock.patch.object(
            reclone_module, "file_hash", side_effect=AssertionError("hashed")
        ):
            rerun = self._run(apply=True)
        self.assertEqual((rerun.identical, rerun.recloned), (0, 0))
        self.assertEqual(rerun.skipped[reclone_module.ALREADY_CLONED], 1)

        # The clone stays independent of its source.
        with source.open("ab") as handle:
            handle.write(b"later\n")
        source_bytes = source.read_bytes()
        self.assertNotEqual(shared.read_bytes(), source_bytes)
        source.unlink()
        self.assertEqual(len(shared.read_bytes()), 512 * 1024)

    @needs_darwin_clone
    def test_darwin_snapshot_zeroed_private_size_still_reclones(self) -> None:
        """The blocker: a snapshot drops every old byte copy's private size to
        0.  That must not read as "already a clone"."""
        source, shared = self._pair("snapshotted", content=os.urandom(256 * 1024))

        def as_if_snapshotted(real):
            return replace(real, private_size=0)

        with storage_overrides({shared: as_if_snapshotted}):
            dry = self._run(apply=False)
            report = self._run(apply=True)

        self.assertEqual(dry.recloned, 1)
        self.assertEqual(dry.skipped[reclone_module.ALREADY_CLONED], 0)
        self.assertEqual(report.recloned, 1)
        self.assertEqual(report.reclaim_now_bytes, 0)
        self.assertEqual(report.reclaim_eventual_bytes, 256 * 1024)
        self.assertTrue(
            apfs.file_storage(shared).is_clone_of(apfs.file_storage(source))
        )
        rerun = self._run(apply=False)
        self.assertEqual(rerun.skipped[reclone_module.ALREADY_CLONED], 1)

    def test_clone_id_decides_already_cloned_not_private_size(self) -> None:
        source, byte_copy = self._pair("byte-copy")
        clone_source, clone = self._pair("clone")
        snapshot_zeroed = apfs.FileStorage(
            fsid=(1, 1), clone_id=11, private_size=0, ext_flags=0x2
        )
        overrides = {
            source: apfs.FileStorage(fsid=(1, 1), clone_id=10, ext_flags=0x2),
            byte_copy: snapshot_zeroed,
            clone_source: apfs.FileStorage(
                fsid=(1, 1), clone_id=20, ext_flags=apfs.EF_SHARES_ALL_BLOCKS
            ),
            clone: apfs.FileStorage(
                fsid=(1, 1),
                clone_id=20,
                private_size=0,
                ext_flags=apfs.EF_SHARES_ALL_BLOCKS | 0x1,
            ),
        }
        hashed: list[Path] = []
        real_hash = reclone_module.file_hash

        def spy_hash(path):
            hashed.append(Path(path))
            return real_hash(path)

        with (
            storage_overrides(overrides),
            mock.patch.object(reclone_module, "file_hash", side_effect=spy_hash),
        ):
            report = self._run(apply=False)

        self.assertEqual(report.recloned, 1)
        self.assertEqual(report.skipped[reclone_module.ALREADY_CLONED], 1)
        self.assertEqual(report.reclaim_now_bytes, 0)
        self.assertEqual(
            report.reclaim_eventual_bytes, byte_copy.stat().st_blocks * 512
        )
        self.assertNotIn(clone, hashed)
        self.assertIn(byte_copy, hashed)
        output = "\n".join(reclone_module.format_report(report))
        self.assertIn("Frees now:", output)
        self.assertIn("Frees up to:", output)
        self.assertIn("tmutil listlocalsnapshots", output)
        self.assertIn("Projected free space after --apply:", output)

    def test_different_real_volumes_are_cross_volume(self) -> None:
        source, shared = self._pair("system-volume")
        overrides = {
            source: apfs.FileStorage(fsid=(16777235, 26), clone_id=1),
            shared: apfs.FileStorage(fsid=(16777229, 26), clone_id=2),
        }
        with storage_overrides(overrides), simulated_clone() as spy:
            report = self._run(apply=True)
        spy.assert_not_called()
        self.assertEqual(report.skipped[reclone_module.CROSS_VOLUME], 1)

    def test_hard_linked_copy_is_skipped_untouched(self) -> None:
        source, shared = self._pair("linked")
        other_name = self.root / "elsewhere-link.jsonl"
        os.link(shared, other_name)
        before = self._snapshot(source, shared, other_name)

        with simulated_clone() as spy:
            report = self._run(apply=True)

        spy.assert_not_called()
        self.assertEqual(report.recloned, 0)
        self.assertEqual(report.skipped[reclone_module.HARD_LINKED], 1)
        self.assertEqual(report.reclaim_eventual_bytes, 0)
        self.assertEqual(self._snapshot(source, shared, other_name), before)
        self.assertEqual(shared.stat().st_nlink, 2)

    def test_source_flags_that_block_cloning_are_predicted_in_dry_run(self) -> None:
        self._pair("locked")
        with (
            simulated_clone() as spy,
            mock.patch.object(
                reclone_module, "_clone_allowed_by_flags", return_value=False
            ),
        ):
            report = self._run(apply=False)
        spy.assert_not_called()
        self.assertEqual(report.recloned, 0)
        self.assertEqual(report.skipped[reclone_module.CLONE_UNSUPPORTED], 1)

    @needs_darwin_clone
    def test_darwin_locked_source_is_never_cloned(self) -> None:
        source, shared = self._pair("locked")
        os.chflags(source, stat.UF_IMMUTABLE)
        self.addCleanup(clear_flags, source)
        before = self._snapshot(shared)
        report = self._run(apply=True)
        self.assertEqual(report.skipped[reclone_module.CLONE_UNSUPPORTED], 1)
        self.assertEqual(report.errors, [])
        self.assertEqual(self._snapshot(shared), before)
        self.assertEqual(self._leftovers(), [])

    def test_copy_moved_away_before_the_swap_is_not_recreated(self) -> None:
        _, shared = self._pair("privatised")
        moved = self.root / "moved-away.jsonl"
        content = shared.read_bytes()

        def move_then_swap(first, second):
            os.replace(second, moved)
            portable_rename_swap(first, second)

        with simulated_clone(swap=move_then_swap):
            report = self._run(apply=True)

        self.assertEqual(report.recloned, 0)
        self.assertEqual(report.skipped[reclone_module.SHARED_CHANGED], 1)
        self.assertFalse(sync_module._lexists(shared))
        self.assertEqual(moved.read_bytes(), content)
        self.assertEqual(self._leftovers(), [])

    def test_private_transition_racing_the_swap_leaves_no_public_copy(self) -> None:
        """A real `logpile private` landing between the identity check and
        the install must not leave the transcript at the public shared path."""
        _, shared = self._pair("secret")
        content = shared.read_bytes()
        conn = sqlite3.connect(self.db_path)
        try:
            # Sessions default to private; this one is shared until the race.
            conn.execute("UPDATE sessions SET visibility = 'unlisted'")
            conn.commit()
        finally:
            conn.close()

        def privatise_then_swap(first, second):
            with get_db(self.db_path) as conn:
                set_session_visibility(
                    conn, "session-1", "private", shared_dir=self.shared
                )
            portable_rename_swap(first, second)

        with simulated_clone(swap=privatise_then_swap):
            report = self._run(apply=True)

        self.assertEqual(report.skipped[reclone_module.SHARED_CHANGED], 1)
        self.assertFalse(sync_module._lexists(shared))
        conn = sqlite3.connect(self.db_path)
        try:
            visibility, archived = conn.execute(
                "SELECT visibility, shared_path FROM sessions WHERE session_id = ?",
                ("session-1",),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(visibility, "private")
        self.assertTrue(reclone_module._is_within(Path(archived), self.private_root))
        self.assertEqual(Path(archived).read_bytes(), content)
        public = [path for path in self.shared.rglob("*") if path.is_file()]
        self.assertEqual(public, [])

    def test_copy_replaced_before_the_swap_gets_its_name_back(self) -> None:
        _, shared = self._pair("republished")
        replacement = b"a newer copy written by a visibility transition\n"

        calls: list[int] = []

        def replace_then_swap(first, second):
            calls.append(1)
            if len(calls) == 1:  # the install, not the swap back
                staged = Path(second).with_name("incoming")
                staged.write_bytes(replacement)
                os.replace(staged, second)
            portable_rename_swap(first, second)

        with simulated_clone(swap=replace_then_swap) as spy:
            report = self._run(apply=True)

        spy.assert_called_once()
        self.assertEqual(len(calls), 2)
        self.assertEqual(report.recloned, 0)
        self.assertEqual(report.skipped[reclone_module.SHARED_CHANGED], 1)
        self.assertEqual(shared.read_bytes(), replacement)
        self.assertEqual(self._leftovers(), [])

    def test_failed_swap_back_keeps_the_replacement(self) -> None:
        _, shared = self._pair("double-race")
        original = shared.read_bytes()
        replacement = b"replacement bytes\n"
        calls: list[int] = []

        def replace_swap_then_fail(first, second):
            calls.append(1)
            if len(calls) == 2:
                raise OSError(errno.EIO, "swap back failed")
            staged = Path(second).with_name("incoming")
            staged.write_bytes(replacement)
            os.replace(staged, second)
            portable_rename_swap(first, second)

        with simulated_clone(swap=replace_swap_then_fail):
            report = self._run(apply=True)

        self.assertEqual(report.skipped[reclone_module.ERROR], 1)
        # The verified clone (the old bytes) sits at the shared path, and the
        # replacement is kept beside it under the staging name, never deleted.
        self.assertEqual(shared.read_bytes(), original)
        kept = self._leftovers()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), replacement)
        self.assertTrue(any("left in place" in error for error in report.errors))

    def test_volume_without_swap_is_skipped_untouched(self) -> None:
        # os.replace could recreate a copy a visibility change moved away, so
        # a volume that cannot swap is skipped, never served by it.
        source, shared = self._pair("no-swap")
        before = self._snapshot(source, shared)
        unsupported = apfs.SwapUnsupported("no swaps here", errno.ENOTSUP)
        with (
            simulated_clone(swap=unsupported) as spy,
            mock.patch.object(
                reclone_module.os, "replace", side_effect=AssertionError("replace")
            ),
        ):
            report = self._run(apply=True)
        spy.assert_called_once()
        self.assertEqual(report.recloned, 0)
        self.assertEqual(report.skipped[reclone_module.CLONE_UNSUPPORTED], 1)
        self.assertEqual(self._snapshot(source, shared), before)
        self.assertEqual(self._leftovers(), [])

    def test_interrupt_after_the_swap_keeps_an_original_changed_in_place(
        self,
    ) -> None:
        """An in-place write to the old copy just before the swap, then an
        interrupt before the swapped-out file is checked: the changed bytes
        must survive (Astra review, finding 2)."""
        _, shared = self._pair("rewritten")
        original = shared.read_bytes()
        rewritten = bytes(reversed(original))

        def rewrite_swap_interrupt(first, second):
            with open(second, "r+b") as handle:  # same inode, new bytes
                handle.write(rewritten)
            portable_rename_swap(first, second)
            raise KeyboardInterrupt

        with (
            simulated_clone(swap=rewrite_swap_interrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._run(apply=True)

        self.assertEqual(shared.read_bytes(), original)  # the verified clone
        kept = self._leftovers()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), rewritten)

    def test_failed_swap_back_keeps_an_original_changed_in_place(self) -> None:
        _, shared = self._pair("rewritten-then-stuck")
        original = shared.read_bytes()
        appended = original + b"written in place\n"
        calls: list[int] = []

        def append_then_swap(first, second):
            calls.append(1)
            if len(calls) == 2:
                raise OSError(errno.EIO, "swap back failed")
            with open(second, "ab") as handle:
                handle.write(b"written in place\n")
            portable_rename_swap(first, second)

        with simulated_clone(swap=append_then_swap):
            report = self._run(apply=True)

        self.assertEqual(report.skipped[reclone_module.ERROR], 1)
        self.assertEqual(shared.read_bytes(), original)
        kept = self._leftovers()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), appended)
        self.assertTrue(any("left in place" in error for error in report.errors))

    def test_reclaim_estimate_is_counted_per_file(self) -> None:
        measured_source, measured = self._pair("measured")
        unmeasured_source, unmeasured = self._pair("unmeasured")
        overrides = {
            measured_source: apfs.FileStorage(fsid=(1, 1), clone_id=3),
            unmeasured_source: apfs.FileStorage(fsid=(1, 1), clone_id=4),
            measured: apfs.FileStorage(fsid=(1, 1), clone_id=1, private_size=4096),
            unmeasured: apfs.FileStorage(fsid=(1, 1), clone_id=2),
        }
        with storage_overrides(overrides):
            report = self._run(apply=False)
        self.assertEqual(report.recloned, 2)
        self.assertEqual(report.reclaim_now_estimated, 1)
        self.assertEqual(
            report.reclaim_now_bytes, 4096 + unmeasured.stat().st_blocks * 512
        )
        self.assertIn(
            "allocated size stands in for 1 file whose volume reported none",
            "\n".join(reclone_module.format_report(report)),
        )

    @needs_darwin_clone
    def test_darwin_sigkill_during_apply_never_leaves_a_partial_copy(self) -> None:
        pairs = [
            self._pair(f"killed-{index:02}", content=os.urandom(1024 * 1024))
            for index in range(48)
        ]
        expected = {shared: source.read_bytes() for source, shared in pairs}
        command = [
            sys.executable,
            "-c",
            "from logpile.cli import cli; cli()",
            "reclone-shared",
            "--apply",
            "--db",
            str(self.db_path),
            "--shared-dir",
            str(self.shared),
        ]

        def assert_intact() -> None:
            for shared, content in expected.items():
                info = shared.lstat()
                self.assertTrue(stat.S_ISREG(info.st_mode), shared)
                self.assertEqual(info.st_mode & 0o777, 0o600, shared)
                self.assertEqual(shared.read_bytes(), content, shared)

        directory = pairs[0][1].parent
        killed_mid_install = 0
        for _ in range(4):
            existing = set(staging_leftovers(directory))
            process = subprocess.Popen(
                command,
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # SIGKILL the moment a new staging file exists: mid-install.
            deadline = time.monotonic() + 60
            while process.poll() is None and time.monotonic() < deadline:
                if set(staging_leftovers(directory)) - existing:
                    process.kill()
                    break
            if process.wait() == -9:
                killed_mid_install += 1
            assert_intact()
        self.assertGreater(killed_mid_install, 0)

        with mock.patch.object(reclone_module, "STAGING_MIN_AGE_SECONDS", 0):
            report = self._run(apply=True)
        self.assertEqual(report.errors, [])
        self.assertEqual(sum(report.staging_kept.values()), 0)
        assert_intact()
        self.assertEqual(self._leftovers(), [])
        for source, shared in pairs:
            self.assertTrue(
                apfs.file_storage(shared).is_clone_of(apfs.file_storage(source))
            )


class StaleStagingSweepTests(_RecloneFixture):
    """reclone-shared removes staging files a killed copy left behind, but
    only ones that are byte-identical to the copy they were staged for."""

    def setUp(self) -> None:
        super().setUp()
        _, self.published = self._pair("session")
        self.directory = self.published.parent

    def _staging(self, token: str, content: bytes | None = None) -> Path:
        path = self.directory / f".{self.published.name}.{token}.tmp-sync"
        path.write_bytes(self.published.read_bytes() if content is None else content)
        path.chmod(0o600)
        return path

    def _run_old_enough(self, *, apply: bool):
        with (
            mock.patch.object(reclone_module, "STAGING_MIN_AGE_SECONDS", 0),
            simulated_clone(),
        ):
            return self._run(apply=apply)

    def test_identical_leftovers_are_reported_then_removed(self) -> None:
        clone_leftover = self._staging("0123456789abcdef")
        byte_copy_leftover = self._staging("ab_cd123")

        dry = self._run_old_enough(apply=False)
        self.assertEqual(dry.staging_removed, 2)
        self.assertEqual(dry.staging_removed_bytes, 2 * self.published.stat().st_size)
        self.assertTrue(clone_leftover.exists())
        self.assertTrue(byte_copy_leftover.exists())
        self.assertIn(
            "Stale staging files (*.tmp-sync): would remove 2",
            "\n".join(reclone_module.format_report(dry)),
        )

        applied = self._run_old_enough(apply=True)
        self.assertEqual(applied.staging_removed, 2)
        self.assertFalse(sync_module._lexists(clone_leftover))
        self.assertFalse(sync_module._lexists(byte_copy_leftover))
        self.assertEqual(applied.errors, [])

    def test_leftovers_that_might_matter_are_kept(self) -> None:
        differs = self._staging("1111111111111111", b"not the published bytes\n")
        orphan = self.directory / ".gone.jsonl.2222222222222222.tmp-sync"
        orphan.write_bytes(b"no published copy\n")
        target = self.root / "outside.jsonl"
        target.write_bytes(self.published.read_bytes())
        link = self.directory / f".{self.published.name}.3333333333333333.tmp-sync"
        link.symlink_to(target)
        directory_clone = (
            self.directory / f".{self.published.name}.4444444444444444.tmp-sync"
        )
        directory_clone.mkdir()
        (directory_clone / "inner").write_bytes(b"x")
        unrelated = self.directory / f".{self.published.name}.12345.0.rollback"
        unrelated.write_bytes(self.published.read_bytes())
        before = self._snapshot(differs, orphan, link, target, unrelated)

        report = self._run_old_enough(apply=True)

        self.assertEqual(report.staging_removed, 0)
        self.assertEqual(
            dict(report.staging_kept),
            {
                reclone_module.STAGING_DIFFERS: 1,
                reclone_module.STAGING_NO_COPY: 1,
                reclone_module.STAGING_NOT_REGULAR: 2,
            },
        )
        self.assertEqual(
            self._snapshot(differs, orphan, link, target, unrelated), before
        )
        self.assertTrue((directory_clone / "inner").exists())

    def test_published_copy_replaced_mid_compare_keeps_the_leftover(self) -> None:
        """Astra review, finding 1: a hash reads an inode it already opened,
        so a copy replaced mid-hash must not vouch for the leftover."""
        leftover = self._staging("8888888888888888")
        leftover_bytes = leftover.read_bytes()
        real_hash = reclone_module.file_hash

        def hash_then_republish(path):
            digest = real_hash(path)
            if Path(path) == self.published:
                replacement = self.published.with_name("republished")
                replacement.write_bytes(b"revision B\n")
                os.replace(replacement, self.published)
            return digest

        with mock.patch.object(
            reclone_module, "file_hash", side_effect=hash_then_republish
        ):
            report = self._run_old_enough(apply=True)

        self.assertEqual(report.staging_removed, 0)
        self.assertEqual(report.staging_kept[reclone_module.STAGING_CHANGED], 1)
        self.assertEqual(leftover.read_bytes(), leftover_bytes)

    def test_recent_leftovers_are_left_for_a_running_copy(self) -> None:
        fresh = self._staging("5555555555555555")
        with simulated_clone():
            report = self._run(apply=True)
        self.assertEqual(report.staging_kept[reclone_module.STAGING_RECENT], 1)
        self.assertTrue(fresh.exists())

    def test_private_archive_leftovers_are_swept_too(self) -> None:
        _, archived = self._pair("archived", root=self.private_root)
        leftover = archived.with_name(f".{archived.name}.6666666666666666.tmp-sync")
        leftover.write_bytes(archived.read_bytes())
        report = self._run_old_enough(apply=True)
        self.assertEqual(report.staging_removed, 1)
        self.assertFalse(leftover.exists())

    @needs_darwin_clone
    def test_darwin_locked_leftover_is_removed(self) -> None:
        # What the pre-fix clone path left behind for a uchg source.
        leftover = self._staging("7777777777777777")
        os.chflags(leftover, stat.UF_IMMUTABLE)
        self.addCleanup(clear_flags, leftover)
        report = self._run_old_enough(apply=True)
        self.assertEqual(report.staging_removed, 1)
        self.assertFalse(sync_module._lexists(leftover))


if __name__ == "__main__":
    unittest.main()
