#SPDX:MIT
"""Test the fileutil module."""
import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from surf_scoter_lib import err, fileutil

POSIX = not fileutil.is_windows()

class LockTest(unittest.TestCase):
    """Test fileutil.exclusive_lock."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(os.path.realpath(self._dir.name))

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_second_lock_waits(self) -> None:
        """A second lock on the file, even in this process, waits and then gives up."""
        path = self.tmp / "lock"
        waited: list[bool] = []
        with (
            fileutil.exclusive_lock(path),
            self.assertRaises(err.LockTimeoutErr),
            fileutil.exclusive_lock(
                path, timeout=0.2, poll=0.05, on_wait=lambda: waited.append(True),
            ),
        ):
            pass
        self.assertEqual(waited, [True])
        # Released: it can be taken again.
        with fileutil.exclusive_lock(path, timeout=0):
            pass

    @unittest.skipUnless(POSIX, "symlinks")
    def test_symlink_refused(self) -> None:
        """The lock file isn't followed through a symlink."""
        target = self.tmp / "target"
        target.write_text("", encoding="utf-8")
        os.symlink(target, self.tmp / "lock")
        with self.assertRaises(OSError), fileutil.exclusive_lock(self.tmp / "lock"):
            pass


class WritePrivateTest(unittest.TestCase):
    """Test fileutil.write_private."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(os.path.realpath(self._dir.name))

    def tearDown(self) -> None:
        self._dir.cleanup()

    @unittest.skipUnless(POSIX, "modes")
    def test_private(self) -> None:
        """Directories 0700, the file 0600, replaced whole."""
        root = self.tmp / "state"
        path = fileutil.write_private(root, ("environments", "host", "suc.json"), b"one")
        self.assertEqual(path, root / "environments" / "host" / "suc.json")
        self.assertEqual(path.read_bytes(), b"one")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        for directory in (root, root / "environments", path.parent):
            with self.subTest(directory=directory):
                self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700, directory)
        fileutil.write_private(root, ("environments", "host", "suc.json"), b"two")
        self.assertEqual(path.read_bytes(), b"two")
        self.assertEqual(sorted(os.listdir(path.parent)), ["suc.json"])

    @unittest.skipUnless(POSIX, "symlinks")
    def test_refused(self) -> None:
        """No symlinked directories, no `..`, and an absolute root."""
        root = self.tmp / "state"
        root.mkdir()
        (self.tmp / "elsewhere").mkdir()
        os.symlink(self.tmp / "elsewhere", root / "environments")
        with self.assertRaises(err.SurfScoterErr):
            fileutil.write_private(root, ("environments", "f"), b"x")
        with self.assertRaises(err.SurfScoterErr):
            fileutil.write_private(root, ("..", "f"), b"x")
        with self.assertRaises(err.SurfScoterErr):
            fileutil.write_private(Path("relative"), ("f",), b"x")



class BackupRestoreTest(unittest.TestCase):
    """Test fileutil.backup and fileutil.restore."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.file = Path(os.path.realpath(self._dir.name)) / "config"
        self.backup = Path(str(self.file) + ".bak")

    @unittest.skipUnless(POSIX, "file modes")
    def test_backup_keeps_mode(self) -> None:
        """The backup has the file's contents and mode."""
        self.file.write_text("original", encoding="utf-8")
        os.chmod(self.file, 0o640)
        self.assertTrue(fileutil.backup(self.file, ".bak"))
        self.assertEqual(self.backup.read_text(encoding="utf-8"), "original")
        self.assertEqual(os.stat(self.backup).st_mode & 0o777, 0o640)

    def test_restore_replaces(self) -> None:
        """The backup replaces the changed file, and is gone."""
        self.file.write_text("original", encoding="utf-8")
        fileutil.backup(self.file, ".bak")
        self.file.write_text("changed", encoding="utf-8")
        self.assertTrue(fileutil.restore(self.file, ".bak"))
        self.assertEqual(self.file.read_text(encoding="utf-8"), "original")
        self.assertFalse(self.backup.exists())

    def test_restore_across_file_systems(self) -> None:
        """Where a rename can't cross file systems (EXDEV), it copies, then removes the backup."""
        self.file.write_text("changed", encoding="utf-8")
        self.backup.write_text("original", encoding="utf-8")
        cross = OSError(errno.EXDEV, "Invalid cross-device link")
        with mock.patch.object(fileutil.os, "replace", side_effect=cross):
            self.assertTrue(fileutil.restore(self.file, ".bak"))
        self.assertEqual(self.file.read_text(encoding="utf-8"), "original")
        self.assertFalse(self.backup.exists())

    def test_restore_fails_otherwise(self) -> None:
        """Any other failure: not restored, and the backup kept."""
        self.file.write_text("changed", encoding="utf-8")
        self.backup.write_text("original", encoding="utf-8")
        with mock.patch.object(fileutil.os, "replace", side_effect=PermissionError(errno.EACCES, "denied")):
            self.assertFalse(fileutil.restore(self.file, ".bak"))
        self.assertEqual(self.file.read_text(encoding="utf-8"), "changed")
        self.assertTrue(self.backup.exists())

    def test_no_backup(self) -> None:
        """No backup: the file is kept, or removed when asked."""
        self.file.write_text("changed", encoding="utf-8")
        self.assertTrue(fileutil.restore(self.file, ".bak"))
        self.assertTrue(self.file.exists())
        self.assertTrue(fileutil.restore(self.file, ".bak", fileutil.WithoutBackup.DELETE))
        self.assertFalse(self.file.exists())
