#SPDX:MIT
"""Test reading the universal configuration file, and the scripts' use of it."""
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from surf_scoter_lib import argutil, universal


class ReadUniversalTest(unittest.TestCase):
    """Test `universal.read_universal`."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "suc.json"

    def test_reads(self) -> None:
        self.path.write_text('{"resolved": {"a": {"values": {"b": "c"}}}}', encoding="utf-8")
        config = universal.read_universal(self.path)
        self.assertEqual(config.raw(), {"resolved": {"a": {"values": {"b": "c"}}}})

    def test_refused(self) -> None:
        """Missing, not JSON, not an object: refused, saying which file and why, never quoting it."""
        cases = [
            (None, "can't read it"),
            ('{"secret-ish": ', "not JSON"),
            ('["secret-ish"]', "not a universal configuration file"),
        ]
        for text, expect in cases:
            with self.subTest(text=text):
                if text is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(universal.UniversalErr) as caught:
                    universal.read_universal(self.path)
                self.assertIn(expect, str(caught.exception))
                self.assertIn(str(self.path), str(caught.exception))
                self.assertNotIn("secret-ish", str(caught.exception))

    def test_bounded(self) -> None:
        """A file larger than the limit is refused, after reading no more than the limit and a byte."""
        self.path.write_text('{"a": "' + "x" * 64 + '"}', encoding="utf-8")
        with (
            mock.patch.object(universal, "MAX_UNIVERSAL_BYTES", 32),
            self.assertRaisesRegex(universal.UniversalErr, "larger than 32 bytes"),
        ):
            universal.read_universal(self.path)


class GenParserTest(unittest.TestCase):
    """Test that the sub-commands read the universal configuration file the same way."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "suc.json"

    def test_reads(self) -> None:
        self.path.write_text('{"resolved": {}}', encoding="utf-8")
        config, rest, _ = argutil.gen_parser(["main.py", str(self.path), "cmd", "--x"], "Test.")
        self.assertIsNotNone(config)
        self.assertEqual(list(rest), ["--x"])

    def test_bad_file(self) -> None:
        """A file that isn't one: no configuration, and the reason on standard error."""
        self.path.write_text('{"a": "' + "x" * 64 + '"}', encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(universal, "MAX_UNIVERSAL_BYTES", 32), redirect_stderr(err):
            config, rest, _ = argutil.gen_parser(["main.py", str(self.path), "cmd"], "Test.")
        self.assertIsNone(config)
        self.assertEqual(list(rest), [])
        self.assertIn("cmd: ", err.getvalue())
        self.assertIn("larger than 32 bytes", err.getvalue())

    def test_not_from_scoter(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            config, _, _ = argutil.gen_parser(["main.py"], "Test.")
        self.assertIsNone(config)
        self.assertIn("Must be run from the scoter script", out.getvalue())
