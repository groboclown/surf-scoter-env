#SPDX:MIT
"""Test the ssh_config parsing of the scoter.py script."""
import unittest

from ...surf_scoter_lib import ssh_config


class SshConfigTest(unittest.TestCase):
    """Unit tests for the ssh_config parser."""

    def test_empty(self) -> None:
        """Test an empty file."""
        config = ssh_config.SshConfig.parse_file("")
        self.assertEqual({}, config.by_host)
        self.assertEqual("", config.output())

    def test_no_host(self) -> None:
        """Test a file with no host value."""
        self.maxDiff = None
        src = """# the file

# text
"""
        config = ssh_config.SshConfig.parse_file(src)
        self.assertEqual(
            {
                None: [
                    ssh_config.SshConfigLine(
                        line="# the file\n", line_no=1, valid=True, host=None, key=None, value=None
                    ),
                    ssh_config.SshConfigLine(
                        line="\n", line_no=2, valid=True, host=None, key=None, value=None
                    ),
                    ssh_config.SshConfigLine(
                        line="# text\n", line_no=3, valid=True, host=None, key=None, value=None
                    ),
                ],
            },
            config.by_host,
        )
        self.assertEqual(src, config.output())

    def test_two_host(self) -> None:
        """Test a file with two host value."""
        self.maxDiff = None
        # Note: no trailing EOL.
        src = """host abc\n  k1 v1\nhost def\n  k2 v2"""
        config = ssh_config.SshConfig.parse_file(src)
        self.assertEqual(
            {
                "abc": [
                    ssh_config.SshConfigLine(
                        line="host abc\n", line_no=1, valid=True,
                        host="abc", key="host", value="abc"
                    ),
                    ssh_config.SshConfigLine(
                        line="  k1 v1\n", line_no=2, valid=True,
                        host="abc", key="k1", value="v1"
                    ),
                ],
                "def": [
                    ssh_config.SshConfigLine(
                        line="host def\n", line_no=3, valid=True,
                        host="def", key="host", value="def"
                    ),
                    ssh_config.SshConfigLine(
                        line="  k2 v2\n", line_no=4, valid=True,
                        host="def", key="k2", value="v2"
                    ),
                ],
            },
            config.by_host,
        )
        self.assertEqual(
            # Note: added trailing EOL.
            src + "\n",
            config.output(),
        )

    def test_output_addl_host_0(self) -> None:
        """Add an extra host + entries to the parsed list."""
        self.maxDiff = None
        src = """# the file

# text
"""
        config = ssh_config.SshConfig.parse_file(src)
        config.add_host("new")
        config.add_key_value("new", "SomeKey", "tuna")
        self.assertEqual(src + "Host new\n  SomeKey tuna\n", config.output())

    def test_output_two_host_introduce_first(self) -> None:
        """Test a file with two host value, inserting a new line in the first one."""
        self.maxDiff = None
        # Note: no trailing EOL.
        src = """host abc\n  k1 v1\nhost def\n  k2 v2"""
        config = ssh_config.SshConfig.parse_file(src)
        config.add_comment("abc", "Add a comment")
        self.assertEqual(
            "host abc\n  k1 v1\n  # Add a comment\nhost def\n  k2 v2\n",
            config.output(),
        )


class RemoveHostTest(unittest.TestCase):
    """Removing a host block."""

    def test_keeps_the_next_blocks_comments(self) -> None:
        """The block goes, with its blank lines; comments after its settings stay, with the block before."""
        config = ssh_config.SshConfig.parse_file(
            "# Top.\n"
            "Host a\n  User me\n\n"
            "Host gone\n  # Surf Scoter environment\n  User x\n\n# About b.\n"
            "Host b\n  User you\n"
        )
        config.remove_host("gone")
        config.remove_host("never-there")
        self.assertEqual(
            config.output(),
            "# Top.\nHost a\n  User me\n\n# About b.\nHost b\n  User you\n",
        )

    def test_first_block(self) -> None:
        """The first block's trailing lines go before the next block."""
        config = ssh_config.SshConfig.parse_file("Host gone\n  User x\n\n# Kept.\nHost b\n  User you\n")
        config.remove_host("gone")
        self.assertEqual(config.output(), "# Kept.\nHost b\n  User you\n")
