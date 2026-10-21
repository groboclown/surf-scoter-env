#SPDX:MIT
"""Test the active environments file."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from ...surf_scoter_lib import fileutil
from ...surf_scoter_lib.active_env import ActiveEnvironmentSet
from ...surf_scoter_lib.err import ActiveEnvErr

POSIX = sys.platform != "win32"


class ActiveEnvTest(unittest.TestCase):
    """Reading, reserving and changing the active environments, under the lock."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.state = Path(os.path.realpath(directory)) / "state"
        self.file = self.state / "running-env.json"
        self.lock = self.state / "running-env.json.lock"

    def locked(self, timeout: float = 5.0) -> contextlib.AbstractContextManager[ActiveEnvironmentSet]:
        self.state.mkdir(exist_ok=True)
        return ActiveEnvironmentSet.locked(self.file, self.lock, timeout=timeout)

    def test_first_time(self) -> None:
        """No state directory yet: it's made, private."""
        with ActiveEnvironmentSet.locked(self.file, self.lock) as envs:
            envs.reserve_environ("h")
        self.assertEqual(len(ActiveEnvironmentSet.read(self.file).environs), 1)
        if POSIX:
            self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)

    def test_empty(self) -> None:
        """No file yet: no environments."""
        self.assertEqual(ActiveEnvironmentSet.read(self.file).environs, {})

    def test_reserve_and_update(self) -> None:
        """A reservation records the port, user, project and process at once; one per host."""
        with self.locked() as envs:
            envs.reserve_environ("scoter-p-0123", port="2222", user="me", project_dir="/p")
        env = ActiveEnvironmentSet.read(self.file).get_environ_by_host("scoter-p-0123")
        assert env is not None  # nosec: B101  # narrowing for the type checker
        self.assertEqual((env.dev_env_ssh_port, env.dev_env_ssh_user, env.pid), ("2222", "me", os.getpid()))
        self.assertEqual(
            [e.host for e in ActiveEnvironmentSet.read(self.file).environs_for_project("/p")], [env.host],
        )
        # A second run of the project is refused, naming the first.
        with self.assertRaisesRegex(ActiveEnvErr, "scoter-p-0123 is already starting"), self.locked() as envs:
            envs.reserve_environ("scoter-p-0123", port="2223", user="me")
        with self.locked() as envs:
            envs.reserve_environ("scoter-q-4567", port="2223", user="me")
        with self.locked() as envs:
            self.assertTrue(envs.rm_environ_by_host(env.host))
            self.assertFalse(envs.rm_environ_by_host(env.host))
        self.assertEqual(list(ActiveEnvironmentSet.read(self.file).environs), ["scoter-q-4567"])

    def test_update_after_removal(self) -> None:
        """Updating an environment someone else removed (`scoter kill`) is an error."""
        with self.locked() as envs:
            envs.reserve_environ("h")
        env = ActiveEnvironmentSet.read(self.file).environs["h"]
        with self.locked() as envs:
            envs.rm_environ_by_host(env.host)
        with self.assertRaisesRegex(ActiveEnvErr, "isn't registered any more"), self.locked() as envs:
            envs.update_environ(env)

    def test_error_changes_nothing(self) -> None:
        """An error in the block leaves the file as it was."""
        with self.locked() as envs:
            envs.reserve_environ("h")
        before = self.file.read_bytes()
        with self.assertRaises(RuntimeError), self.locked() as envs:
            envs.reserve_environ("other")
            raise RuntimeError("stop")
        self.assertEqual(self.file.read_bytes(), before)

    @unittest.skipUnless(POSIX, "modes")
    def test_private(self) -> None:
        """It holds whole configuration files, so it's written private."""
        self.state.mkdir(mode=0o755)
        os.chmod(self.state, 0o755)  # nosec: B103  # To see it made private.
        self.file.write_text("[]", encoding="utf-8")
        os.chmod(self.file, 0o644)  # nosec: B103  # As an earlier version left it.
        with self.locked() as envs:
            envs.reserve_environ("h")
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)

    @unittest.skipUnless(POSIX, "links")
    def test_link_refused(self) -> None:
        """A link in its place isn't followed."""
        self.state.mkdir()
        target = self.state / "elsewhere.json"
        target.write_text("[]", encoding="utf-8")
        os.symlink(target, self.file)
        with self.assertRaisesRegex(ActiveEnvErr, "can't be read"):
            ActiveEnvironmentSet.read(self.file)

    def test_held_lock_times_out(self) -> None:
        """While another command holds the lock, a change waits, then gives up."""
        self.state.mkdir()
        # Entered in order: the other command's lock first.
        with (
            fileutil.exclusive_lock(self.lock),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(ActiveEnvErr, "held the lock"),
            self.locked(timeout=0.3),
        ):
            pass
        # Once it's let go, it's free: nothing is left held.
        with self.locked() as envs:
            envs.reserve_environ("h")

    @unittest.skipUnless(POSIX, "modes")
    def test_old_lock_file(self) -> None:
        """An earlier version's read-only lock file, left behind by a crash, doesn't stop it."""
        self.state.mkdir()
        self.lock.write_text("[]", encoding="utf-8")
        os.chmod(self.lock, 0o400)
        with self.locked() as envs:
            envs.reserve_environ("h")
        self.assertEqual(len(json.loads(self.file.read_text())), 1)
