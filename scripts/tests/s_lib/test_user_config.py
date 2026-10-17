#SPDX:MIT
"""Test the suc module."""
import unittest
import os
from pathlib import Path
import tempfile

from ...surf_scoter_lib import user_config


class UserConfigTest(unittest.TestCase):
    """Test the UserConfig class."""

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
