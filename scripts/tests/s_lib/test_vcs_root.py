#SPDX:MIT
"""Test the vcs_root module."""
import os
import tempfile
import unittest
from pathlib import Path

from ...surf_scoter_lib import vcs_root


class VcsRootTest(unittest.TestCase):
    """Test finding a repository's root from its files."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self._tmp.name))
        self.home = self.dir / "me"
        self.home.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def mkdirs(self, *paths: str) -> None:
        """Make directories under the temporary directory."""
        for path in paths:
            (self.dir / path).mkdir(parents=True, exist_ok=True)

    def test_nearest(self) -> None:
        """The nearest directory with the marker: a directory, or a submodule's file."""
        self.mkdirs("repo/.git", "repo/a/b", "repo/sub/c")
        (self.dir / "repo/sub/.git").write_text("gitdir: ../.git/modules/sub\n")
        self.assertEqual(vcs_root.nearest(".git", self.dir / "repo/a/b", self.home), self.dir / "repo")
        self.assertEqual(vcs_root.nearest(".git", self.dir / "repo", self.home), self.dir / "repo")
        self.assertEqual(
            vcs_root.nearest(".git", self.dir / "repo/sub/c", self.home), self.dir / "repo/sub"
        )
        self.assertIsNone(vcs_root.nearest(".hg", self.dir / "repo/a", self.home))

    def test_outermost(self) -> None:
        """The highest of the unbroken run with the marker, from the nearest up."""
        self.mkdirs("top/CVS", "top/mod/CVS", "top/mod/sub/CVS", "top/mod/sub/new")
        self.assertEqual(
            vcs_root.outermost("CVS", self.dir / "top/mod/sub", self.home), self.dir / "top"
        )
        # A directory not yet added starts the run from the next one up.
        self.assertEqual(
            vcs_root.outermost("CVS", self.dir / "top/mod/sub/new", self.home), self.dir / "top"
        )
        # A gap ends the run.
        self.mkdirs("gap/CVS", "gap/none/inner/CVS", "gap/none/inner/x/CVS")
        self.assertEqual(
            vcs_root.outermost("CVS", self.dir / "gap/none/inner/x", self.home),
            self.dir / "gap/none/inner",
        )
        # A directory not yet added, inside a checkout, belongs to it.
        self.assertEqual(
            vcs_root.outermost("CVS", self.dir / "gap/none", self.home), self.dir / "gap"
        )

    def test_stops_below_home(self) -> None:
        """A home directory kept in a repository isn't the project's repository."""
        self.mkdirs("me/.git", "me/work/proj")
        self.assertIsNone(vcs_root.nearest(".git", self.dir / "me/work/proj", self.home))
        self.mkdirs("me/work/.git")
        self.assertEqual(
            vcs_root.nearest(".git", self.dir / "me/work/proj", self.home), self.dir / "me/work"
        )

    def test_check_marker(self) -> None:
        """A marker is a single name."""
        self.assertIsNone(vcs_root.check_marker(".git"))
        for bad in ["", ".", "..", "a/b", "a\\b"]:
            with self.subTest(bad=bad):
                self.assertIsNotNone(vcs_root.check_marker(bad), bad)
