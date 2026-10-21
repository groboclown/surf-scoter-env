#SPDX:MIT
"""Test the verified store module."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ....surf_scoter_lib import err
from ....surf_scoter_lib.user_config import UserConfig
from ....surf_scoter_lib.verified import store
from .helper import POSIX, TempDirTest


class EnvironmentIdTest(unittest.TestCase):
    """Test environment_id."""

    def test_id(self) -> None:
        """The name, cleaned up, and a hash of the canonical path."""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "My Project_2"
            project.mkdir()
            env_id = store.environment_id(project)
            name, digest = env_id.rsplit("-", 1)
            self.assertEqual(name, "my-project-2")
            self.assertEqual(len(digest), 12)
            # The same directory, however it's written.
            self.assertEqual(store.environment_id(project / ".." / "My Project_2"), env_id)
            other = Path(tmp) / "x" / "My Project_2"
            other.mkdir(parents=True)
            self.assertNotEqual(store.environment_id(other), env_id)
            weird = Path(tmp) / "___"
            weird.mkdir()
            self.assertTrue(store.environment_id(weird).startswith("project-"))
            long_name = Path(tmp) / ("a" * 50)
            long_name.mkdir()
            self.assertEqual(len(store.environment_id(long_name)), 32 + 1 + 12)

    def test_matches_the_generator(self) -> None:
        """The same as the generator's `scoter:project-id` for these (host/src/config/system.rs)."""
        for directory, expected in (
            ("/no/such/My Project!! (copy)", "my-project-copy-c593c7ec2d55"),
            ("/no/such/" + "x" * 40 + "-y", "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx-06211fd163da"),
            ("/no/such/!!!", "project-b94ae0807dcf"),
        ):
            self.assertEqual(store.environment_id(Path(directory)), expected, directory)

    def test_project_id_from_the_universal_file(self) -> None:
        """The generator's id when the file has one; worked out the same way when it hasn't."""
        given = UserConfig({"active-config-locations": {"project-id": "given-0123456789ab"}})
        self.assertEqual(store.project_id(given, "/no/such/x"), "given-0123456789ab")
        self.assertEqual(store.project_id(UserConfig({}), "/no/such/!!!"), "project-b94ae0807dcf")


@unittest.skipUnless(POSIX, "owner and mode checks")
class StoreTest(TempDirTest):
    """Test VerifiedStore."""

    def test_write_and_read(self) -> None:
        """Files are written whole, private, and read back."""
        verified = self.new_store()
        with verified.locked():
            path = verified.write_json("env", "f.json", data={"$schema": "s", "a": 1})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)
        self.assertEqual(verified.read_json("env", "f.json", schema="s"), {"$schema": "s", "a": 1})
        self.assertIsNone(verified.read_json("env", "missing.json", schema="s"))
        with self.assertRaises(err.VerifiedStoreErr):
            verified.read_json("env", "f.json", schema="other")

    def test_needs_the_lock(self) -> None:
        """Changes need the lock."""
        verified = self.new_store()
        with self.assertRaises(RuntimeError):
            verified.write_bytes("f", data=b"")

    def test_wrong_mode(self) -> None:
        """A file or directory others can use is refused."""
        verified = self.new_store()
        with verified.locked():
            path = verified.write_bytes("env", "f", data=b"x")
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(err.VerifiedStoreErr, "others can use it"):
            verified.read_bytes("env", "f", max_size=10)
        os.chmod(path, 0o600)
        os.chmod(path.parent, 0o755)  # nosec: B103  # Testing a store others can use.
        with self.assertRaisesRegex(err.VerifiedStoreErr, "others can use it"):
            verified.read_bytes("env", "f", max_size=10)
        os.chmod(self.tmp / "verified", 0o750)  # nosec: B103  # Testing a store others can use.
        with self.assertRaisesRegex(err.VerifiedStoreErr, "others can use it"):
            store.VerifiedStore.open(self.tmp / "verified")

    def test_wrong_owner(self) -> None:
        """A file another user owns is refused."""
        verified = self.new_store()
        with verified.locked():
            verified.write_bytes("f", data=b"x")
        with (
            mock.patch.object(store.os, "getuid", return_value=os.getuid() + 1),
            self.assertRaisesRegex(err.VerifiedStoreErr, "belongs to another user"),
        ):
            verified.read_bytes("f", max_size=10)

    def test_symlinks_refused(self) -> None:
        """A symlinked file or directory in the store is refused."""
        verified = self.new_store()
        outside = self.write("outside/f", "x", 0o600)
        os.chmod(outside.parent, 0o700)
        os.symlink(outside.parent, self.tmp / "verified" / "env")
        with self.assertRaisesRegex(err.VerifiedStoreErr, "is a symlink"):
            verified.read_bytes("env", "f", max_size=10)
        os.symlink(outside, self.tmp / "verified" / "g")
        with self.assertRaisesRegex(err.VerifiedStoreErr, "can't be opened"):
            verified.read_bytes("g", max_size=10)

    def test_size_limit(self) -> None:
        """A file over the limit is refused."""
        verified = self.new_store()
        with verified.locked():
            verified.write_bytes("f", data=b"12345")
        with self.assertRaisesRegex(err.VerifiedStoreErr, "larger than 4 bytes"):
            verified.read_bytes("f", max_size=4)

    def test_bad_names(self) -> None:
        """Store paths are plain names."""
        verified = self.new_store()
        for name in ("..", "", "a/b", "c:d"):
            with self.assertRaises(err.VerifiedStoreErr, msg=name):
                verified.read_bytes(name, max_size=10)

    def test_relative_root(self) -> None:
        """The store must be absolute."""
        with self.assertRaises(err.VerifiedStoreErr):
            store.VerifiedStore.open(Path("relative"))

    def test_blobs(self) -> None:
        """Blobs are kept by hash, checked when read, and removed when unused."""
        verified = self.new_store()
        with verified.locked():
            one = verified.put_blob(b"one")
            two = verified.put_blob(b"two")
            self.assertEqual(verified.put_blob(b"one"), one)
        self.assertEqual(verified.get_blob(one, 10), b"one")
        self.assertEqual(verified.blob_size(), 6)
        path = self.tmp / "verified" / "blobs" / two[:2] / two
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(err.VerifiedStoreErr, "has changed"):
            verified.get_blob(two, 10)
        with self.assertRaises(err.VerifiedStoreErr):
            verified.get_blob("../x", 10)
        with verified.locked():
            self.assertEqual(verified.remove_unused_blobs({one}), 1)
        self.assertFalse(path.exists())

    def test_index(self) -> None:
        """The index maps each project directory to one id, and each id to one directory."""
        verified = self.new_store()
        with verified.locked():
            verified.register("/a", "a-1")
            verified.register("/a", "a-1")
            verified.register("/b", "b-2")
            with self.assertRaisesRegex(err.VerifiedStoreErr, "lists /a as `a-1`"):
                verified.register("/a", "a-3")
            with self.assertRaisesRegex(err.VerifiedStoreErr, "already /b"):
                verified.register("/c", "b-2")
            verified.unregister("/b")
        self.assertEqual(verified.index(), {"/a": "a-1"})

    def test_two_writers(self) -> None:
        """Two commands registering at once, each with the lock, both get in."""
        verified = self.new_store()
        start = threading.Barrier(2)
        problems: list[BaseException] = []

        def register(path: str, env_id: str) -> None:
            try:
                other = store.VerifiedStore.open(self.tmp / "verified")
                start.wait()
                with other.locked(timeout=10):
                    index = other.index()
                    # Long enough for the other to try.
                    threading.Event().wait(0.1)
                    del index
                    other.register(path, env_id)
            except BaseException as problem:  # noqa: BLE001  # Reported below.
                problems.append(problem)

        quiet = mock.patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)
        threads = [
            threading.Thread(target=register, args=("/a", "a-1")),
            threading.Thread(target=register, args=("/b", "b-2")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(problems, [])
        self.assertEqual(verified.index(), {"/a": "a-1", "/b": "b-2"})
