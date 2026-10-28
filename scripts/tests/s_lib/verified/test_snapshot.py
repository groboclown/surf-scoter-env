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
        """Comments and blank lines go; heredocs, directives and trailing spaces stay."""
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
            "FROM scratch   \n"
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

    def test_dockerfile_line_breaks(self) -> None:
        """Docker breaks lines only on `\\n`: anything Python would also break on has no canonical form (VER-1)."""
        accepted = "FROM scratch\nRUN echo ok\n"
        for hidden in ["\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\x00", "\x1b", "\x7f"]:
            text = f"FROM scratch\nRUN echo ok{hidden}# ; curl https://x | sh\n"
            with self.subTest(hidden=hidden):
                self.assertIsNone(snapshot.canonical_dockerfile(text))
        # A byte order mark at the start is Docker's to ignore; anywhere else, refused.
        self.assertEqual(snapshot.canonical_dockerfile("\ufeff" + accepted), accepted)
        self.assertIsNone(snapshot.canonical_dockerfile("FROM scratch\nRUN \ufeffecho\n"))
        # CRLF line endings are a `\\n` each to Docker.
        self.assertEqual(snapshot.canonical_dockerfile(accepted.replace("\n", "\r\n")), accepted)
        # A Unicode space isn't blank to Docker: the line stays.
        self.assertEqual(snapshot.canonical_dockerfile("FROM a\n\xa0\n"), "FROM a\n\xa0\n")

    def test_dockerfile_heredocs_it_cant_follow(self) -> None:
        """A `<<` that isn't a heredoc it can follow, or one left open, has no canonical form."""
        for text in [
            'RUN <<"END-1"\n# script\nEND-1\n',
            "RUN <<\\EOF\n# script\nEOF\n",
            'RUN echo "a << b"\n',
            "RUN cat <<EOF <<-'END.X'\nx\nEOF\n",
            "RUN <<EOF\n# never closed\n",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(snapshot.canonical_dockerfile(text))
        # Two it can follow, one after the other.
        text = "RUN cat <<A <<-'B'\n# a\nA\n\t# b\n\tB\n"
        self.assertEqual(snapshot.canonical_dockerfile(text), text)

    def test_dockerfile_what_still_matches(self) -> None:
        """Comment and blank-line changes match; whitespace and directive changes don't."""
        base = "# syntax=docker/dockerfile:1\nFROM a\nRUN x \\\n  y\n"
        canonical = snapshot.canonical_dockerfile
        self.assertEqual(canonical(base), canonical(
            "# syntax=docker/dockerfile:1\n# new\n\nFROM a\n\t# here\nRUN x \\\n  # and here\n  y\n\n"
        ))
        self.assertNotEqual(canonical(base), canonical(base.replace("FROM a", "FROM a ")))
        self.assertNotEqual(canonical(base), canonical(base.replace("# syntax=", "#syntax=")))
        self.assertIsNone(canonical("# escape=x\nFROM a\n"))

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
        # Only the Dockerfile the build reads (VER-1).
        self.assertIsNone(hashed("env/Dockerfile.sh", b"#!/bin/sh\necho"))
        self.assertIsNone(hashed("env/x.Dockerfile", b"FROM a\n"))
        self.assertIsNone(hashed("env/Dockerfile", b"FROM a\nRUN ok\r# hidden\n"))
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
            with self.subTest(path=path), self.assertRaises(snapshot.SnapshotErr, msg=path):
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
