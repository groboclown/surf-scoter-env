#SPDX:MIT
"""Test the review module."""
import datetime
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from ...surf_scoter_lib import checks, jdata, review
from ...surf_scoter_lib.user_config import UserConfig

POSIX = sys.platform != "win32"
NOW = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=datetime.UTC)


class CleanTest(unittest.TestCase):
    """Test the text cleaning and the YAML pieces."""

    def test_clean(self) -> None:
        """Control characters and escapes go; anything outside ASCII shows escaped."""
        self.assertEqual(review.clean("a\nb\tc\r"), "a b c ")
        self.assertEqual(review.clean("\x1b[31mred\x1b[0m"), "red")
        self.assertEqual(review.clean("\x1b]0;title\x07after"), "after")
        self.assertEqual(review.clean("bell\x07 del\x7f c1\x9b"), "bell del c1")
        # A right-to-left override, a zero-width space, a Cyrillic `\u0430`.
        self.assertEqual(
            review.clean("x\u202ey\u200bz \u0430"),
            "x\\u202ey\\u200bz \\u0430",
        )
        self.assertEqual(review.clean("emoji \U0001f600"), "emoji \\U0001f600")

    def test_scalars(self) -> None:
        """Text as JSON writes it: one line, ASCII, escaped."""
        self.assertEqual(review.scalar("x\n# reviewed: safe"), '"x\\n# reviewed: safe"')
        self.assertEqual(review.scalar("\u202e"), '"\\u202e"')
        self.assertEqual(review.scalar(443), "443")
        self.assertEqual(review.scalar(None), "null")
        self.assertEqual(review.key("command"), "command")
        self.assertEqual(review.key("$override"), "$override")
        self.assertEqual(review.key("AWS_*"), '"AWS_*"')
        self.assertEqual(review.key("a: b"), '"a: b"')
        self.assertEqual(
            review.flow(["s3", {"any": {"prefix": "s3://b/"}}]),
            '["s3", {any: {prefix: "s3://b/"}}]',
        )


@unittest.skipUnless(POSIX, "modes")
class BuildTest(unittest.TestCase):
    """Test the review file's layout."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(os.path.realpath(directory))
        os.chmod(self.tmp, 0o700)
        self.project = self.tmp / "proj"
        files = self.project / ".surf-scoter.d"
        (files / "env").mkdir(parents=True)
        (files / "project.yaml").write_text("x: 1\n", encoding="utf-8")
        (files / "env" / "Dockerfile").write_text(
            "FROM debian:bookworm\n# \u202e hidden \u200b\nRUN \x1b[2Jtrue\n", encoding="utf-8",
        )
        self.bin = self.tmp / "bin"
        self.bin.mkdir(mode=0o700)
        for name in ("aws", "sh"):
            path = self.bin / name
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(path, 0o700)

    def suc(self) -> jdata.DictJsonData:
        plan = {"allow": [], "drop": [], "error": [], "set": {}, "default": {}}
        return {
            "active-config-locations": {
                "project-dir": str(self.project),
                "dev-container-path": str(self.project / ".surf-scoter.d" / "env"),
            },
            "resolved": {
                "scoter": {"values": {
                    "project-files-dir": ".surf-scoter.d", "project-file-name": "project.yaml",
                    "verified-expiry-days": 10,
                }},
                "network-gateway": {"values": {"name": "squid"}},
            },
            "current-project-setup": {
                "egress": [{"host": "registry.npmjs.org", "protocol": "tls"}],
                "env": {"set": {"CONF": "${ssh:config-file}", "NOTE": "x\n# reviewed: safe"}},
                "commands": [
                    {"command": "aws", "args": ["s3", {"any": {"prefix": "s3://b/"}}],
                     "env": {"allow": ["AWS_*"]}},
                    {"command": "sh", "args": ["-c", {"any": {}}]},
                    {"command": "gone", "args": []},
                ],
            },
            "current-project-resolved": {
                "env": {**plan, "set": {"CONF": "/home/u/.ssh/config", "NOTE": "x\n# reviewed: safe"}},
                "commands": [
                    {"env": {**plan, "allow": ["AWS_*"]}},
                    {"env": plan},
                    {"env": plan},
                ],
            },
            "host-command-checks": {"checks": {
                "host-bridge:shells": {
                    "files": [], "action": "refuse",
                    "reason": "A shell runs anything. \x1b[31mDanger\x1b[0m \u202e",
                    "commands": {"sh": str(self.bin / "sh")}, "name-patterns": [], "options": [],
                    "env": [], "paths": [], "overrides": [],
                },
            }, "command-map": {}},
            "dev-container": {"parent-builds": []},
        }

    def build(self) -> str:
        config = UserConfig(self.suc())
        report = checks.check_project(config, str(self.bin))
        context = review.Context(
            report=report, now=NOW, files=review.project_snapshot(config), branch="main",
        )
        return review.build(config, context)

    def test_layout(self) -> None:
        """The data, with the findings as comments."""
        text = self.build()
        lines = text.splitlines()
        self.assertIn('review: "surf-scoter-review-1"', lines)
        self.assertIn("# result: reject", lines)
        self.assertIn(f'project-dir: "{self.project}"', lines)
        self.assertIn('expires-at: "2026-10-30T12:00:00+00:00"', lines)
        self.assertIn('  - {host: "registry.npmjs.org", protocol: "tls", port: 443}', lines)
        self.assertIn("#scoter:   branch         main", lines)
        self.assertIn("#scoter:   gateway        squid", lines)
        # The accepted command; the refused one commented out, with its override;
        # the missing one commented out, without.
        self.assertIn(f'    program: "{self.bin / "aws"}"', lines)
        self.assertIn('    args: ["s3", {any: {prefix: "s3://b/"}}]', lines)
        self.assertIn('  # - command: "sh"', lines)
        self.assertIn('    # $override: ["host-bridge:shells"]', lines)
        self.assertIn('  # - command: "gone"', lines)
        gone = text[text.index('# - command: "gone"'):]
        self.assertNotIn("$override", gone.split("\n\n")[0])
        # `${...}` values resolved, with their source.
        self.assertIn("    #scoter: from `${ssh:config-file}`", lines)
        self.assertIn('    CONF: "/home/u/.ssh/config"', lines)
        self.assertIn('  use: "project"', lines)
        self.assertIn('    "debian:bookworm": "debian:bookworm"', lines)

    def test_values_cant_pose_as_comments(self) -> None:
        """A newline in a value stays inside its quotes, on one line."""
        text = self.build()
        self.assertIn('    NOTE: "x\\n# reviewed: safe"', text.splitlines())
        self.assertFalse(any(line.lstrip().startswith("# reviewed") for line in text.splitlines()))

    def test_nothing_hides(self) -> None:
        """Bidirectional and zero-width characters show escaped; terminal escapes go."""
        text = self.build()
        self.assertTrue(text.isascii())
        self.assertNotIn("\x1b", text)
        self.assertIn("\\u202e hidden \\u200b", text)
        self.assertIn("A shell runs anything. Danger \\u202e", text)
        self.assertIn("  #scoter:     | RUN true", text.splitlines())


class BranchTest(unittest.TestCase):
    """Test reading the current branch from a Git checkout's files."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(directory)

    def test_branch(self) -> None:
        from ...surf_scoter_lib import vcs_branch
        (self.tmp / ".git").mkdir()
        (self.tmp / ".git" / "HEAD").write_text("ref: refs/heads/feature/x\n", encoding="utf-8")
        self.assertEqual(vcs_branch.current_branch(self.tmp), "feature/x")
        (self.tmp / ".git" / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
        self.assertEqual(vcs_branch.current_branch(self.tmp), "detached at " + "a" * 12)
        (self.tmp / ".git" / "HEAD").write_text("nonsense\n", encoding="utf-8")
        self.assertIsNone(vcs_branch.current_branch(self.tmp))

    def test_worktree_file(self) -> None:
        """A `.git` file names the Git directory."""
        from ...surf_scoter_lib import vcs_branch
        real = self.tmp / "real-git"
        real.mkdir()
        (real / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        checkout = self.tmp / "checkout"
        checkout.mkdir()
        (checkout / ".git").write_text("gitdir: ../real-git\n", encoding="utf-8")
        self.assertEqual(vcs_branch.current_branch(checkout), "main")
        self.assertIsNone(vcs_branch.current_branch(self.tmp / "nothing"))
