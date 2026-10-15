"""Test the configuration loading and accessing."""

import unittest
import configparser
import os
import shutil
import tempfile
from .fixtures import scoter_lib


class ConfigTest(unittest.TestCase):
    """Test the config loading."""

    def test_replace(self) -> None:
        """Test replacements."""
        config_file = os.path.join(self.tmpdir, scoter_lib.SCOTER_DEFAULT_INI_FILE)
        c_p = configparser.ConfigParser()
        c_p["base"] = {"aa": "11", "bb": "22", "cc": "-{aa}-{bb}-"}
        c_p["sub"] = {"aa": "33", "bb": "44", "cc": "-{aa}-{bb}-{base:aa}-{base:bb}-{base:cc}-"}
        c_p["recurse"] = {"aa": "-{aa}-"}
        with open(config_file, "w", encoding="utf-8") as fos:
            c_p.write(fos)

        config = scoter_lib.UserConfig(config_file, None)
        data: list[tuple[str, str, str]] = [
            ("base", "aa", "11"),
            ("base", "bb", "22"),
            ("base", "cc", "-11-22-"),
            ("sub", "aa", "33"),
            ("sub", "bb", "44"),
            ("sub", "cc", "-33-44-11-22--11-22--"),
        ]
        for sec, key, exp in data:
            with self.subTest(f"{sec}_{key}"):
                self.assertEqual(
                    exp,
                    config.require_str(sec, key),
                )

        with self.subTest("recurse_aa"):
            try:
                config.get_str("recurse", "aa", "-")
                self.fail("did not raise IniError")
            except scoter_lib.IniError as err:
                self.assertTrue(repr({"recurse:aa"}) in str(err), err)

    def test_load_file_defaults(self) -> None:
        """Test loading the file defaults.
        
        This goes under the assumption that the scoter_lib file reads its location
        and tries that as an install path.
        """
        config = scoter_lib.UserConfig(None, scoter_lib.find_global_config_path())
        # Ensure the ssh path comes back as expected.
        self.assertEqual(
            "~/.ssh",
            config.require_str("ssh", "home"),
        )
        self.assertEqual(
            os.path.expanduser("~/.ssh"),
            str(config.require_path("ssh", "home"))
        )

    def setUp(self) -> None:
        """Setup a single test case."""
        self.tmpdir = tempfile.mkdtemp()
        self._orig_home = os.environ.get(scoter_lib.ENV_SCOTER_HOME)

    def tearDown(self) -> None:
        """Tear down a single test case."""
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._orig_home is not None:
            os.environ[scoter_lib.ENV_SCOTER_HOME] = self._orig_home
        elif scoter_lib.ENV_SCOTER_HOME in os.environ:
            del os.environ[scoter_lib.ENV_SCOTER_HOME]
