#SPDX:MIT
"""Test the suc module."""
import os
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

