#SPDX:MIT
"""Test the suc module."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

from ...surf_scoter_lib import user_config


class UserConfigTest(unittest.TestCase):
    """Test the UserConfig class."""

    def test_active_location(self) -> None:
        """A location that isn't set is null, or missing; either is None."""
        config = user_config.UserConfig({
            "active-config-locations": {"project-dir": "/proj", "cwd": None, "bad": 5},
        })
        self.assertEqual(config.active_location("project-dir"), Path("/proj"))
        self.assertIsNone(config.active_location("cwd"))
        self.assertIsNone(config.active_location("missing"))
        with self.assertRaises(user_config.ConfigErr):
            config.active_location("bad")
        self.assertIsNone(user_config.UserConfig({}).active_location("cwd"))

    def test_read(self) -> None:
        """Test the read function for a valid file."""
        fp, file = tempfile.mkstemp()
        try:
            os.write(fp, b'{"resolved":{"some-vcs":{}}}')
            os.close(fp)
            config = user_config.UserConfig.read(Path(file))
            self.assertEqual("some-vcs", config.get_vcs_section_for("some"))
        finally:
            os.unlink(file)

    def test_project(self) -> None:
        """The project file is read from `current-project-setup`, the commands from `host-brokered`."""
        config = user_config.UserConfig({
            "current-project-setup": {"vcs": "git", "commands": [{"command": "ignored"}]},
            "host-brokered": [{"command": "aws"}, {"command": "git"}, {"command": "aws"}],
        })
        self.assertEqual(config.project_str("vcs"), "git")
        self.assertEqual(config.project_bridge_commands(), ["aws", "git"])
        empty = user_config.UserConfig({"current-project-setup": None, "host-brokered": []})
        self.assertIsNone(empty.project_str("vcs"))
        self.assertEqual(empty.project_bridge_commands(), [])



class DropEnvTest(unittest.TestCase):
    """Test the environment a section's commands get."""

    def test_drop_env(self) -> None:
        """Names drop exactly; a `*` drops a prefix."""
        environ = {"GIT_DIR": "x", "GIT_CONFIG_KEY_0": "y", "GITHUB_USER": "u", "HOME": "/h", "PATH": "/b"}
        self.assertEqual(
            user_config.drop_env(environ, ["GIT_*", "HOME"]), {"GITHUB_USER": "u", "PATH": "/b"},
        )
        self.assertEqual(user_config.drop_env(environ, []), environ)

    def test_run_command(self) -> None:
        """A section's `runs` commands run without its `drop-env` variables; others' keep them."""
        show = [sys.executable, "-c", "import os; print(os.environ.get('GIT_DIR', '-'))"]
        config = user_config.UserConfig({"resolved": {
            "git-vcs": {"runs": {"show": show}, "value-lists": {"drop-env": ["GIT_*"]}},
            "other": {"runs": {"show": show}},
        }})
        old = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = "/elsewhere/.git"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                dropped = config.run_command("git-vcs", "show").stdout
                kept = config.run_command("other", "show").stdout
        finally:
            if old is None:
                del os.environ["GIT_DIR"]
            else:
                os.environ["GIT_DIR"] = old
        self.assertEqual(dropped.strip(), b"-")
        self.assertEqual(kept.strip(), b"/elsewhere/.git")


class RequireValueTest(unittest.TestCase):
    """A section's values, as text."""

    def test_text_and_numbers(self) -> None:
        config = user_config.UserConfig({"resolved": {"dev-env": {"values": {
            "image": "x", "cpus": 4, "ratio": 0.5, "flag": True, "unset": None,
        }}}})
        self.assertEqual(config.require_value("dev-env", "image"), "x")
        self.assertEqual(config.require_value("dev-env", "cpus"), "4")
        self.assertEqual(config.require_value("dev-env", "ratio"), "0.5")
        for name in ("flag", "unset", "missing"):
            with self.assertRaises(user_config.ConfigErr, msg=name):
                config.require_value("dev-env", name)

    def test_sshd_port(self) -> None:
        """Null until the run chooses it; a number or text after."""
        config = user_config.UserConfig({"dev-container": {"sshd-port": None}})
        self.assertIsNone(config.dev_container_sshd_port())
        config.set_dev_container_sshd_port("20001")
        self.assertEqual(config.dev_container_sshd_port(), "20001")
        self.assertEqual(user_config.UserConfig({"dev-container": {"sshd-port": "22"}}).dev_container_sshd_port(), "22")
