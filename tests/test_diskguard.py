import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from logpile import diskguard
from logpile.diskguard import GIB, DiskGuardPolicy, check_disk_space


def usage(free_gib: float):
    return lambda _path: SimpleNamespace(free=int(free_gib * GIB))


class DiskGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_allows_sync_above_plain_floor_without_snapshots(self):
        decision = check_disk_space(
            [self.root],
            DiskGuardPolicy.from_gib(40, 60),
            disk_usage=usage(45),
            snapshot_lister=lambda: (),
        )
        self.assertTrue(decision.ok)
        self.assertEqual(decision.floor_bytes, 40 * GIB)
        self.assertEqual(decision.snapshot_count, 0)

    def test_defers_below_plain_floor(self):
        decision = check_disk_space(
            [self.root],
            DiskGuardPolicy.from_gib(40, 60),
            disk_usage=usage(16),
            snapshot_lister=lambda: (),
        )
        self.assertFalse(decision.ok)
        self.assertIn("16.0 GiB free", decision.reason)
        self.assertIn("40.0 GiB free-space floor", decision.reason)

    def test_local_snapshot_raises_the_floor(self):
        snapshots = ("com.apple.TimeMachine.2026-09-29-185337.local",)
        decision = check_disk_space(
            [self.root],
            DiskGuardPolicy.from_gib(40, 60),
            disk_usage=usage(50),
            snapshot_lister=lambda: snapshots,
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.floor_bytes, 60 * GIB)
        self.assertEqual(decision.snapshot_count, 1)
        self.assertIn("Time Machine local snapshot", decision.reason)

    def test_unknown_snapshots_apply_only_the_plain_floor(self):
        decision = check_disk_space(
            [self.root],
            DiskGuardPolicy.from_gib(40, 60),
            disk_usage=usage(50),
            snapshot_lister=lambda: None,
        )
        self.assertTrue(decision.ok)
        self.assertIsNone(decision.snapshot_count)

    def test_reused_snapshot_answer_skips_the_lister(self):
        lister = mock.Mock(side_effect=AssertionError("must not fork tmutil"))
        decision = check_disk_space(
            [self.root],
            DiskGuardPolicy.from_gib(40, 60),
            disk_usage=usage(70),
            snapshot_lister=lister,
            snapshots=("com.apple.TimeMachine.x.local",),
            snapshots_known=True,
        )
        self.assertTrue(decision.ok)
        self.assertEqual(decision.floor_bytes, 60 * GIB)

    def test_tightest_volume_decides(self):
        other = self.root / "other"
        other.mkdir()
        frees = {self.root: 100 * GIB, other: 10 * GIB}
        with mock.patch.object(
            diskguard.os,
            "stat",
            side_effect=lambda p, **_kwargs: SimpleNamespace(
                st_dev=1 if Path(p) == self.root else 2
            ),
        ):
            decision = check_disk_space(
                [self.root, other],
                DiskGuardPolicy.from_gib(40, 60),
                disk_usage=lambda p: SimpleNamespace(free=frees[Path(p)]),
                snapshot_lister=lambda: (),
            )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.path, other)

    def test_missing_path_measures_nearest_existing_ancestor(self):
        seen = []

        def record(path):
            seen.append(Path(path))
            return SimpleNamespace(free=80 * GIB)

        check_disk_space(
            [self.root / "not" / "yet" / "logpile.db"],
            DiskGuardPolicy.from_gib(40, 60),
            disk_usage=record,
            snapshot_lister=lambda: (),
        )
        self.assertEqual(seen, [self.root])

    def test_disabled_policy_always_allows(self):
        decision = check_disk_space(
            [self.root],
            DiskGuardPolicy.disabled(),
            disk_usage=usage(0),
            snapshot_lister=lambda: ("com.apple.TimeMachine.x.local",),
        )
        self.assertTrue(decision.ok)

    def test_negative_floor_is_rejected(self):
        with self.assertRaises(ValueError):
            DiskGuardPolicy.from_gib(-1, 60)

    def test_snapshot_listing_parses_tmutil_output(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "Snapshots for disk /:\n"
                "com.apple.TimeMachine.2026-09-29-185337.local\n"
                "com.apple.TimeMachine.2026-09-29-195337.local\n"
            ),
            stderr="",
        )
        with (
            mock.patch.object(diskguard.sys, "platform", "darwin"),
            mock.patch.object(diskguard.subprocess, "run", return_value=completed),
        ):
            self.assertEqual(
                diskguard.list_local_snapshots(),
                (
                    "com.apple.TimeMachine.2026-09-29-185337.local",
                    "com.apple.TimeMachine.2026-09-29-195337.local",
                ),
            )

    def test_snapshot_listing_failure_is_unknown(self):
        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="x"
        )
        with (
            mock.patch.object(diskguard.sys, "platform", "darwin"),
            mock.patch.object(diskguard.subprocess, "run", return_value=failed),
        ):
            self.assertIsNone(diskguard.list_local_snapshots())
        with (
            mock.patch.object(diskguard.sys, "platform", "darwin"),
            mock.patch.object(
                diskguard.subprocess, "run", side_effect=OSError("no tmutil")
            ),
        ):
            self.assertIsNone(diskguard.list_local_snapshots())

    def test_non_macos_reports_unknown_snapshots(self):
        with mock.patch.object(diskguard.sys, "platform", "linux"):
            self.assertIsNone(diskguard.list_local_snapshots())


if __name__ == "__main__":
    unittest.main()
