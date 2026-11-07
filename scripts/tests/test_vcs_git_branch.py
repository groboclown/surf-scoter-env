#SPDX:MIT
"""Test the Git branch handler (`vcs_git_branch.py`), the `git-vcs` section's `runs: current-branch`."""
import shutil
import subprocess  # nosec: B404  # Runs the handler, as a list.
import sys
import tempfile
import unittest
from pathlib import Path

from ..surf_scoter_lib import fileutil

SCRIPTS = Path(__file__).parents[1]
HANDLER = SCRIPTS / "vcs_git_branch.py"


def _branch(root: Path) -> subprocess.CompletedProcess[str]:
    # As `python: runs: script` runs it: the script's directory has the library.
    return subprocess.run(  # nosec: B603  # The handler, as a list.
        [sys.executable, "-E", "-s", str(HANDLER), str(root)],
        capture_output=True, text=True, check=False,
    )


@unittest.skipUnless(not fileutil.is_windows(), "modes")
class BranchTest(unittest.TestCase):
    """Reading the current branch from a Git checkout's files."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(directory)

    def assert_prints(self, root: Path, expected: str) -> None:
        res = _branch(root)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, expected, ""))

    def test_branch(self) -> None:
        """A branch, a detached commit, and a `HEAD` it can't read."""
        (self.tmp / ".git").mkdir()
        head = self.tmp / ".git" / "HEAD"
        head.write_text("ref: refs/heads/feature/x\n", encoding="utf-8")
        self.assert_prints(self.tmp, "feature/x\n")
        head.write_text("a" * 40 + "\n", encoding="utf-8")
        self.assert_prints(self.tmp, "detached at " + "a" * 12 + "\n")
        head.write_text("nonsense\n", encoding="utf-8")
        self.assert_prints(self.tmp, "")

    def test_worktree_file(self) -> None:
        """A `.git` file names the Git directory; no `.git` prints nothing."""
        real = self.tmp / "real-git"
        real.mkdir()
        (real / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        checkout = self.tmp / "checkout"
        checkout.mkdir()
        (checkout / ".git").write_text("gitdir: ../real-git\n", encoding="utf-8")
        self.assert_prints(checkout, "main\n")
        self.assert_prints(self.tmp / "nothing", "")

    def test_symlinked_head(self) -> None:
        """A symlinked `HEAD` isn't followed."""
        (self.tmp / ".git").mkdir()
        (self.tmp / "elsewhere").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (self.tmp / ".git" / "HEAD").symlink_to(self.tmp / "elsewhere")
        self.assert_prints(self.tmp, "")

    def test_usage(self) -> None:
        """Anything but one argument is a usage error."""
        res = subprocess.run(  # nosec: B603  # The handler, as a list.
            [sys.executable, "-E", "-s", str(HANDLER)], capture_output=True, text=True, check=False,
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn("Usage:", res.stderr)
