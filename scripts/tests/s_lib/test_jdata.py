#SPDX:MIT
"""Test the jdata module."""
import unittest
from typing import ClassVar

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


class FindTest(unittest.TestCase):
    """Test the find_* lookups: missing or null is None; the wrong type is an error."""

    DATA: ClassVar[jdata.DictJsonData] = {
        "a": {"b": "text", "n": 3, "t": True, "z": None, "l": ["x", "y"], "m": {"k": 1}},
        "list": [{"v": "first"}],
    }

    def test_found(self) -> None:
        self.assertEqual(jdata.find_str(self.DATA, "a", "b"), "text")
        self.assertEqual(jdata.find_int(self.DATA, "a", "n"), 3)
        self.assertEqual(jdata.find_bool(self.DATA, "a", "t"), True)
        self.assertEqual(jdata.find_dict(self.DATA, "a", "m"), {"k": 1})
        self.assertEqual(jdata.find_list(self.DATA, "a", "l"), ["x", "y"])
        self.assertEqual(jdata.find_str_list(self.DATA, "a", "l"), ["x", "y"])
        self.assertEqual(jdata.find_str(self.DATA, "list", 0, "v"), "first")
        self.assertEqual(jdata.find_dict_keys(self.DATA, "a", "m"), [("a", "m", "k")])
        self.assertEqual(jdata.find_list_keys(self.DATA, "a", "l"), [("a", "l", 0), ("a", "l", 1)])

    def test_not_there(self) -> None:
        for path in (("missing",), ("a", "missing"), ("a", "z"), ("a", "z", "deeper"), ("list", 5)):
            self.assertIsNone(jdata.find(self.DATA, *path), path)
            self.assertIsNone(jdata.find_str(self.DATA, *path), path)
        self.assertEqual(jdata.find_str_list(self.DATA, "missing"), [])
        self.assertEqual(jdata.find_dict_keys(self.DATA, "a", "z"), [])
        self.assertEqual(jdata.find_list_keys(self.DATA, "nope"), [])

    def test_wrong_type(self) -> None:
        for call in (
            lambda: jdata.find_str(self.DATA, "a", "n"),
            lambda: jdata.find_int(self.DATA, "a", "t"),
            lambda: jdata.find_bool(self.DATA, "a", "n"),
            lambda: jdata.find_dict(self.DATA, "a", "l"),
            lambda: jdata.find_list(self.DATA, "a", "m"),
            lambda: jdata.find_str_list(self.DATA, "a", "m"),
            lambda: jdata.find_str_list({"l": ["x", 1]}, "l"),
            # A path through text, or a key into a list.
            lambda: jdata.find(self.DATA, "a", "b", "c"),
            lambda: jdata.find(self.DATA, "list", "v"),
        ):
            with self.assertRaises(TypeError):
                call()
