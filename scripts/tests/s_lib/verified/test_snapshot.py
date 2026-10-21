#SPDX:MIT
"""Test the verified snapshot module."""
import os
import unittest
from pathlib import Path

from ....surf_scoter_lib.verified import snapshot, store
from .helper import POSIX, TempDirTest


class CanonicalTest(unittest.TestCase):
    """Test the canonical forms."""

    def test_dockerfile(self) -> None:
        """Comments, blank lines and trailing spaces go; heredocs and directives stay."""
        text = (
            "# syntax=docker/dockerfile:1\n"
            "# A comment.\n"
            "FROM scratch   \n"
            "\n"
            "RUN a \\\n"
            "    # inside a continuation\n"
            "    b <<EOF\n"
            "# kept: it's the script\n"
            "\n"
            "EOF\n"
            "  # indented comment\n"
            "COPY x /x\n"
        )
        self.assertEqual(
            snapshot.canonical_dockerfile(text),
            "# syntax=docker/dockerfile:1\n"
            "FROM scratch\n"
            "RUN a \\\n"
            "    b <<EOF\n"
            "# kept: it's the script\n"
            "\n"
            "EOF\n"
            "COPY x /x\n",
        )

    def test_dockerfile_escape(self) -> None:
        """The escape directive sets the continuation character."""
        text = "# escape=`\nRUN a `\n  <<EOF\n# script\nEOF\n"
        self.assertEqual(snapshot.canonical_dockerfile(text), text)

    def test_hashes(self) -> None:
        """Which files get a canonical hash, and what it ignores."""
        parsed = snapshot.ParsedFiles(project={"a": 1}, dev_container={"b": 2})

        def hashed(rel: str, data: bytes) -> str | None:
            return snapshot.canonical_hash(rel, data, parsed, "project.yaml")

        self.assertEqual(hashed("project.yaml", b"# x\na: 1\n"), store.json_id({"a": 1}))
        self.assertEqual(hashed("env/dev-env.yaml", b"anything"), store.json_id({"b": 2}))
        self.assertEqual(hashed("env/Dockerfile", b"FROM a\n"), hashed("env/Dockerfile", b"# c\nFROM a\n"))
        self.assertEqual(hashed("x.json", b'{"b": 1, "a": 2}'), hashed("x.json", b'{"a":2,"b":1}'))
        self.assertIsNone(hashed("x.json", b'{"a": 1, "a": 2}'))
        self.assertIsNone(hashed("x.json", b"NaN"))
        self.assertIsNone(hashed("env/script.sh", b"echo"))
        self.assertIsNone(hashed("other.yaml", b"a: 1"))


class SnapshotTest(TempDirTest):
    """Test taking and restoring snapshots."""

    LIMITS = snapshot.SnapshotLimits(max_file_size=1000, max_snapshot_size=2000)

    def project(self) -> Path:
        files = self.tmp / "proj" / ".surf-scoter.d"
        self.write("proj/.surf-scoter.d/project.yaml", "container: {dev-container: env}\n")
        self.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n")
        self.write("proj/.surf-scoter.d/env/dev-env.yaml", "user: me\n")
        self.write("proj/.surf-scoter.d/env/bin/run.sh", "#!/bin/sh\n", 0o755)
        self.write("proj/.surf-scoter.d/unused/Dockerfile", "FROM x\n")
        return files

    def take(self, files: Path, extra: list[Path] | None = None) -> snapshot.Snapshot:
        return snapshot.snapshot_project(
            files, "project.yaml", [files / "env", *(extra or [])],
            snapshot.ParsedFiles(project={"p": 1}, dev_container={"d": 1}), self.LIMITS,
        )

    def test_snapshot(self) -> None:
        """The project file and the build's container directories; nothing else."""
        files = self.project()
        outside = self.tmp / "dist" / "containers" / "base"
        outside.mkdir(parents=True)
        taken = self.take(files, [outside])
        manifest = taken.manifest
        self.assertEqual(
            sorted(manifest.files),
            ["env/Dockerfile", "env/bin/run.sh", "env/dev-env.yaml", "project.yaml"],
        )
        self.assertTrue(manifest.files["env/bin/run.sh"].executable)
        self.assertFalse(manifest.files["project.yaml"].executable)
        self.assertEqual(manifest.files["project.yaml"].canonical, store.json_id({"p": 1}))
        self.assertIsNone(manifest.files["env/bin/run.sh"].canonical)
        self.assertEqual(snapshot.Manifest.from_json(manifest.to_json()), manifest)

    def test_noise(self) -> None:
        """A comment changes the id, but not the meaning."""
        files = self.project()
        before = self.take(files).manifest
        self.write("proj/.surf-scoter.d/env/Dockerfile", "# new comment\nFROM scratch\n")
        after = self.take(files).manifest
        self.assertNotEqual(before.id, after.id)
        self.assertEqual(before.meaning_id, after.meaning_id)
        self.assertEqual(before.changed(after), [])
        self.write("proj/.surf-scoter.d/env/bin/run.sh", "#!/bin/sh\necho\n", 0o755)
        changed = self.take(files).manifest
        self.assertNotEqual(before.meaning_id, changed.meaning_id)
        self.assertEqual(before.changed(changed), ["env/bin/run.sh"])

    @unittest.skipUnless(POSIX, "symlinks, hard links and FIFOs")
    def test_refused(self) -> None:
        """Symlinks, hard links and special files in a container directory."""
        files = self.project()
        link = files / "env" / "creds"
        os.symlink(self.tmp / "secret", link)
        with self.assertRaisesRegex(snapshot.SnapshotErr, "a symlink"):
            self.take(files)
        link.unlink()
        os.link(files / "env" / "Dockerfile", self.tmp / "elsewhere")
        with self.assertRaisesRegex(snapshot.SnapshotErr, "other hard links"):
            self.take(files)
        (self.tmp / "elsewhere").unlink()
        os.mkfifo(files / "env" / "pipe")
        with self.assertRaisesRegex(snapshot.SnapshotErr, "a special file"):
            self.take(files)

    @unittest.skipUnless(POSIX, "symlinks and hard links")
    def test_project_file(self) -> None:
        """A symlinked project file is refused; a hard-linked one warns."""
        files = self.project()
        os.link(files / "project.yaml", self.tmp / "elsewhere")
        self.assertEqual(len(self.take(files).warnings), 1)
        (files / "project.yaml").unlink()
        os.symlink(self.tmp / "elsewhere", files / "project.yaml")
        with self.assertRaisesRegex(snapshot.SnapshotErr, "may be a symlink"):
            self.take(files)

    def test_limits(self) -> None:
        """Each file, and all of them, are limited."""
        files = self.project()
        self.write("proj/.surf-scoter.d/env/big", "x" * 1001)
        with self.assertRaisesRegex(snapshot.SnapshotErr, "larger than the 1000 bytes"):
            self.take(files)
        self.write("proj/.surf-scoter.d/env/big", "x" * 990)
        self.write("proj/.surf-scoter.d/env/big2", "x" * 990)
        with self.assertRaisesRegex(snapshot.SnapshotErr, "larger than the 2000 bytes"):
            self.take(files)

    @unittest.skipUnless(POSIX, "owner and mode checks")
    def test_restore(self) -> None:
        """A stored snapshot restores into an empty private directory."""
        files = self.project()
        verified = self.new_store()
        taken = self.take(files)
        with verified.locked():
            manifest = taken.store(verified)
        dest = self.tmp / "staging"
        dest.mkdir(mode=0o700)
        snapshot.restore(verified, manifest, dest, self.LIMITS)
        self.assertEqual((dest / "env" / "Dockerfile").read_text(), "FROM scratch\n")
        # The usual modes, which a build's `COPY` keeps, inside the private directory.
        self.assertEqual(os.stat(dest / "env" / "bin" / "run.sh").st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(dest / "env" / "Dockerfile").st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(dest / "env" / "bin").st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(dest).st_mode & 0o777, 0o700)
        with self.assertRaisesRegex(snapshot.SnapshotErr, "not empty"):
            snapshot.restore(verified, manifest, dest, self.LIMITS)

    def test_bad_paths(self) -> None:
        """A manifest path that could lead outside the snapshot is refused."""
        for path in ("../x", "/etc/passwd", "a/../../b", "a//b", "./a", "a\\b", "c:x", ""):
            with self.assertRaises(snapshot.SnapshotErr, msg=path):
                snapshot.check_relative(path)
        entry = snapshot.FileEntry(sha256="0" * 64, size=1, executable=False).to_json()
        data = {"id": "x", "files": {"../escape": entry}}
        with self.assertRaises(snapshot.SnapshotErr):
            snapshot.Manifest.from_json(data)

    def test_tampered_manifest(self) -> None:
        """A manifest whose id doesn't match its files is refused."""
        manifest = snapshot.Manifest({
            "a": snapshot.FileEntry(sha256="0" * 64, size=1, executable=False),
        }).to_json()
        manifest["id"] = "sha256:other"
        with self.assertRaisesRegex(ValueError, "doesn't match"):
            snapshot.Manifest.from_json(manifest)
