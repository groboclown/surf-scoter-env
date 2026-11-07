#SPDX:MIT
"""Test `scoter push` (`pull.push`), with the real generator."""
import os
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
from surf_scoter_lib import generator, run_config
from surf_scoter_lib.pull import lifecycle, push

from .dist_helper import HAVE_GENERATOR, NEEDS_GENERATOR
from .test_lifecycle import LifecycleBase


@unittest.skipUnless(HAVE_GENERATOR, NEEDS_GENERATOR)
class PushTest(LifecycleBase):
    """Writing the configuration in use back into the project."""

    def edit_and_lock(self, text: str = "FROM scratch\nRUN edited\n") -> None:
        """Pull, unlock, change the Dockerfile, and lock."""
        self.pull()
        root = lifecycle.unlock(self.config())
        (root / "files" / "env" / "Dockerfile").write_text(text, encoding="utf-8")
        self.assertEqual(self.lock(), 0, self.lines)

    def push(self, *, yes: bool = True, answer: str = "") -> int:
        return push.push(self.config(), yes=yes, ask=lambda _: answer, say=self.lines.append)

    def test_nothing_then_push(self) -> None:
        """Nothing to push at first; after an edit, the patch, the write, and the base moved."""
        self.pull()
        self.assertEqual(self.push(), 0)
        self.assertIn("Nothing to push", self.lines[-1])
        self.edit_and_lock()
        self.lines.clear()
        self.assertEqual(self.push(yes=False), 1)
        self.assertIn("diff --git a/.surf-scoter.d/env/Dockerfile b/.surf-scoter.d/env/Dockerfile", self.lines)
        self.assertIn("+RUN edited", self.lines)
        self.assertEqual((self.files / "env" / "Dockerfile").read_text(encoding="utf-8"), "FROM scratch\n")
        self.assertEqual(self.push(yes=False, answer="yes"), 0, self.lines)
        self.assertEqual((self.files / "env" / "Dockerfile").read_text(encoding="utf-8"), "FROM scratch\nRUN edited\n")
        self.assertFalse((self.files / "host.json").exists())
        # The run doesn't take the developer's own push for a change in the project (VER-21).
        dest = self.tmp / "staged"
        dest.mkdir(mode=0o700)
        prepared = run_config.prepare(self.config(), dest, generate=generator.run, path_env=self.path_env)
        self.assertEqual(prepared.notices, [])

    def test_changed_after_the_patch(self) -> None:
        """A file that changed after the patch was made isn't overwritten."""
        self.edit_and_lock()
        planned = push.plan(self.config())
        (self.files / "env" / "Dockerfile").write_text("FROM someone-else\n", encoding="utf-8")
        result = push.write(self.config(), planned)
        self.assertEqual(result.refused, ["env/Dockerfile"])
        self.assertEqual((self.files / "env" / "Dockerfile").read_text(encoding="utf-8"), "FROM someone-else\n")

    def test_extras_and_refusals(self) -> None:
        """Extra files are left; symlinks stop the push; an unlocked configuration isn't pushed."""
        self.edit_and_lock()
        (self.files / "notes.txt").write_text("mine\n", encoding="utf-8")
        self.assertEqual(self.push(), 0, self.lines)
        self.assertTrue((self.files / "notes.txt").is_file())
        self.assertTrue(any("Left: notes.txt" in line for line in self.lines))
        os.symlink("/etc/passwd", self.files / "env" / "creds")
        with self.assertRaisesRegex(push.PushErr, "can't be read safely"):
            push.plan(self.config())
        os.unlink(self.files / "env" / "creds")
        lifecycle.unlock(self.config())
        with self.assertRaisesRegex(push.PushErr, "unlocked"):
            push.plan(self.config())

    def test_symlinked_files_dir(self) -> None:
        self.pull()
        moved = self.tmp / "elsewhere"
        os.rename(self.files, moved)
        os.symlink(moved, self.files)
        with self.assertRaisesRegex(push.PushErr, "symlink"):
            push.plan(self.config())

    def test_warnings(self) -> None:
        """A line naming the home directory is warned of, even with --yes (VER-16)."""
        self.edit_and_lock(f"FROM scratch\nCOPY {os.path.expanduser('~')}/key /key\n")
        self.assertEqual(self.push(), 0, self.lines)
        self.assertTrue(any(line.startswith("Warning:") and "home directory" in line for line in self.lines))

    def test_first_push(self) -> None:
        """After `init`, the first push creates the project files directory, without the host file (VER-39)."""
        os.rename(self.files, self.tmp / "moved")
        self.assertEqual(lifecycle.init(self.config("start"), say=self.lines.append), 0)
        # The template assumes Git; this project isn't in a repository, so it's edited, as a developer would.
        project_file = self.root / "files" / "project.yaml"
        project_file.write_text(project_file.read_text(encoding="utf-8").replace("vcs: git", "vcs: file"), encoding="utf-8")
        self.assertEqual(self.lock(), 0, self.lines)
        self.assertEqual(self.push(), 0, self.lines)
        self.assertTrue((self.files / "project.yaml").is_file())
        self.assertTrue((self.files / "dev-container" / "Dockerfile").is_file())
        self.assertFalse((self.files / "host.json").exists())
        self.assertEqual(os.stat(self.files).st_mode & 0o777, 0o755)
        self.assertFalse([p for p in os.listdir(self.project) if p.startswith(push.TEMP_PREFIX)])
