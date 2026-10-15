#SPDX:MIT
"""Test the ssh_config parsing of the scoter.py script."""

import unittest
from .fixtures import scoter_lib


class SshConfigTest(unittest.TestCase):
    """Unit tests for the ssh_config parser."""

    def test_empty(self) -> None:
        """Test an empty file."""
        by_host = scoter_lib.SshConfigLine.parse_file("")
        self.assertEqual({}, by_host)
        self.assertEqual("", scoter_lib.SshConfigLine.output(by_host))

    def test_no_host(self) -> None:
        """Test a file with no host value."""
        self.maxDiff = None
        src = """# the file

# text
"""
        by_host = scoter_lib.SshConfigLine.parse_file(src)
        self.assertEqual(
            {
                None: [
                    scoter_lib.SshConfigLine(
                        line="# the file\n", line_no=1, valid=True, host=None, key=None, value=None
                    ),
                    scoter_lib.SshConfigLine(
                        line="\n", line_no=2, valid=True, host=None, key=None, value=None
                    ),
                    scoter_lib.SshConfigLine(
                        line="# text\n", line_no=3, valid=True, host=None, key=None, value=None
                    ),
                ],
            },
            by_host,
        )
        self.assertEqual(src, scoter_lib.SshConfigLine.output(by_host))

    def test_two_host(self) -> None:
        """Test a file with two host value."""
        self.maxDiff = None
        # Note: no trailing EOL.
        src = """host abc\n  k1 v1\nhost def\n  k2 v2"""
        by_host = scoter_lib.SshConfigLine.parse_file(src)
        self.assertEqual(
            {
                "abc": [
                    scoter_lib.SshConfigLine(
                        line="host abc\n", line_no=1, valid=True,
                        host="abc", key="host", value="abc"
                    ),
                    scoter_lib.SshConfigLine(
                        line="  k1 v1\n", line_no=2, valid=True,
                        host="abc", key="k1", value="v1"
                    ),
                ],
                "def": [
                    scoter_lib.SshConfigLine(
                        line="host def\n", line_no=3, valid=True,
                        host="def", key="host", value="def"
                    ),
                    scoter_lib.SshConfigLine(
                        line="  k2 v2\n", line_no=4, valid=True,
                        host="def", key="k2", value="v2"
                    ),
                ],
            },
            by_host,
        )
        self.assertEqual(
            # Note: added trailing EOL.
            src + "\n",
            scoter_lib.SshConfigLine.output(by_host),
        )

    def test_output_addl_host_0(self) -> None:
        """Add an extra host + entries to the parsed list."""
        self.maxDiff = None
        src = """# the file

# text
"""
        by_host = scoter_lib.SshConfigLine.parse_file(src)
        by_host["new"] = [
            scoter_lib.SshConfigLine(
                line="Host new\n", line_no=-1, valid=True,
                host="new", key="Host", value="new"
            ),
            scoter_lib.SshConfigLine(
                line="SomeKey tuna\n", line_no=-2, valid=True,
                host="new", key="SomeKey", value="tuna"
            ),
        ]
        self.assertEqual(src + "Host new\nSomeKey tuna\n", scoter_lib.SshConfigLine.output(by_host))

    def test_output_two_host_introduce_first(self) -> None:
        """Test a file with two host value, inserting a new line in the first one."""
        self.maxDiff = None
        # Note: no trailing EOL.
        src = """host abc\n  k1 v1\nhost def\n  k2 v2"""
        by_host = scoter_lib.SshConfigLine.parse_file(src)
        by_host["abc"].append(
            scoter_lib.SshConfigLine(
                line="# Add a comment\n", line_no=-1, valid=True, host="abc", key=None, value=None
            )
        )
        self.assertEqual(
            "host abc\n  k1 v1\n# Add a comment\nhost def\n  k2 v2\n",
            scoter_lib.SshConfigLine.output(by_host),
        )
