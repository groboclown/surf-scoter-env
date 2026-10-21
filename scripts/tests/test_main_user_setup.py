#SPDX:MIT
"""Test the main_user_setup script's ssh files, for a user who has none yet."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# The scripts import the library as their own directory's, as they do when
# they run.
SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
import main_user_setup
from surf_scoter_lib.user_config import UserConfig

# Writes the key pair `-f` names, as ssh-keygen would.
KEYGEN = "import sys; f = sys.argv[sys.argv.index('-f') + 1]; open(f, 'w').write('k'); open(f + '.pub', 'w').write('p')"


@unittest.skipIf(sys.platform == "win32", "modes")
class FreshHomeTest(unittest.TestCase):
    """`scoter user-setup --non-interactive` on a home without `~/.ssh`."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.ssh = Path(os.path.realpath(directory)) / "home" / ".ssh"
        self.config = UserConfig({"resolved": {"ssh": {
            "paths": {
                "key-file": str(self.ssh / "scoter-dev-container"),
                "config-file": str(self.ssh / "config"),
                "known-hosts": str(self.ssh / "known_hosts"),
            },
            "runs": {"mk-key": [sys.executable, "-c", KEYGEN, "-f", str(self.ssh / "scoter-dev-container")]},
        }}})

    def test_creates_the_ssh_files(self) -> None:
        """The key pair, an empty config and known hosts, in a new `~/.ssh` only you can read."""
        with contextlib.redirect_stdout(io.StringIO()):
            main_user_setup.gen_key_pair(self.config, interactive=False)
            main_user_setup.ensure_ssh_config(self.config, interactive=False)
            main_user_setup.ensure_ssh_known_hosts(self.config, interactive=False)
        self.assertEqual(os.stat(self.ssh).st_mode & 0o777, 0o700)
        for name in ("scoter-dev-container", "scoter-dev-container.pub", "config", "known_hosts"):
            self.assertTrue((self.ssh / name).is_file(), name)
        self.assertEqual(os.stat(self.ssh / "config").st_mode & 0o777, 0o600)

    def test_existing_directory_left_alone(self) -> None:
        self.ssh.mkdir(parents=True, mode=0o750)
        os.chmod(self.ssh, 0o750)  # nosec: B103  # A directory of the user's own choosing, to see it kept.
        main_user_setup.ensure_private_dir(self.ssh)
        self.assertEqual(os.stat(self.ssh).st_mode & 0o777, 0o750)


if __name__ == "__main__":
    unittest.main()
