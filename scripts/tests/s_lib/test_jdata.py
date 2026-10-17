#SPDX:MIT
"""Test the jdata module."""
import unittest
from ...surf_scoter_lib import jdata


class JDataTest(unittest.TestCase):
    """Test the functions in the jdata module."""

    def test_path_to_str(self) -> None:
        """Test the path_to_str function."""
        data: list[tuple[jdata.JsonPath, str]] = [
            ([], ""),
            (["a"], "a"),
            ([1], "1"),
            (["aa", "bb"], "aa / bb"),
        ]
        for path, exp in data:
            with self.subTest(exp):
                self.assertEqual(
                    exp,
                    jdata.path_to_str(*path),
                )
