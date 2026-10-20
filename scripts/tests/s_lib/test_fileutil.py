#SPDX:MIT
"""Test the fileutil module."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from ...surf_scoter_lib import err, fileutil

POSIX = sys.platform != "win32"

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
