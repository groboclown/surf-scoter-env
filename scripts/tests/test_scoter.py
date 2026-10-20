#SPDX:MIT
"""Test the scoter front-end script."""
import os
import unittest
from pathlib import Path

import scoter


def _config(sub_command: object) -> scoter.DictJsonData:
    """A universal configuration with the python sub-command run as given."""
    return {"resolved": {"python": {"runs": {"sub-command": sub_command}}}}  # type: ignore[dict-item]


class SubCommandPrefixTest(unittest.TestCase):
    """Test how the sub-command scripts are started."""

    script = Path("/dist/bin/main_run.py")
    python = os.path.abspath("/usr/bin/python3")

    def test_from_the_setup(self) -> None:
        """The setup's interpreter and arguments are used, with the script in place."""
        self.assertEqual(
            scoter.sub_command_prefix(
                _config([self.python, "-E", "-s", "{x:script}"]), self.script
            ),
            [self.python, "-E", "-s", str(self.script)],
        )

    def test_interpreter_not_found(self) -> None:
        """A missing interpreter leaves the command without a value."""
        with self.assertRaisesRegex(scoter.ConfigErr, "No Python interpreter"):
            scoter.sub_command_prefix(_config(None), self.script)
        with self.assertRaisesRegex(scoter.ConfigErr, "No Python interpreter"):
            scoter.sub_command_prefix({"resolved": {}}, self.script)

    def test_bad_commands(self) -> None:
        """The command must be text, name the script, and start with an absolute interpreter."""
        for bad, message in [
            ("python3 {x:script}", "to be a command"),
            ([], "to be a command"),
            ([self.python, 5, "{x:script}"], "to be a command"),
            ([self.python, "-E"], "must include `{x:script}`"),
            (["python3", "{x:script}"], "absolute path"),
        ]:
            with self.subTest(bad=bad), self.assertRaisesRegex(scoter.ConfigErr, message):
                scoter.sub_command_prefix(_config(bad), self.script)
