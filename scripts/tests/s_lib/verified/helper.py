#SPDX:MIT
"""Shared helpers for the verified store tests."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from ....surf_scoter_lib.verified import store

POSIX = sys.platform != "win32"


class TempDirTest(unittest.TestCase):
    """A private temporary directory for each test."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(os.path.realpath(self._dir.name))
        os.chmod(self.tmp, 0o700)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def write(self, rel: str, text: str, mode: int = 0o644) -> Path:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def new_store(self) -> store.VerifiedStore:
        return store.VerifiedStore.open(self.tmp / "verified", create=True)
