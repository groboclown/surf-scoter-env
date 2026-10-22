#SPDX:MIT
"""Test the gateway_squid script."""
import json
import os
import re
import shutil
import subprocess  # nosec: B404  # Runs the gateway script under test.
import sys
import tempfile
import unittest
from pathlib import Path

# The scripts import the library as their own directory's, as they do when
# they run.
SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
import gateway_squid
from surf_scoter_lib import fileutil

SCRIPT = SCRIPTS / "gateway_squid.py"


def _rule(host: str, protocol: str = "tls", **extra: object) -> dict[str, object]:
    return {"host": host, "protocol": protocol, **extra}


class TranslateTest(unittest.TestCase):
    """Test the translation into Squid's host list."""

    def test_enforced_and_refused(self) -> None:
        """Only `tls` on 443; everything else refused, with its reason."""
        rules = gateway_squid.gateway.rules_from(gateway_squid.UserConfig({"egress": {"rules": [
            _rule("registry.npmjs.org"),
            _rule("*.pkg.dev"),
            _rule("a.example.com", protocol="https", methods=["GET"]),
            _rule("b.example.com", protocol="http", methods=["GET"]),
            _rule("git.example.com", port=22),
        ]}}))
        report, text = gateway_squid.translate(rules)
        self.assertEqual([r.host for r in report.enforced], ["registry.npmjs.org", "*.pkg.dev"])
        reasons = {rule.host: reason for rule, reason in report.refused}
        self.assertIn("looks inside TLS", reasons["a.example.com"])
        self.assertIn("port 80", reasons["b.example.com"])
        self.assertIn("only lets TCP port 443 out, not 22", reasons["git.example.com"])
        lines = [line for line in text.splitlines() if not line.startswith("#")]
        self.assertEqual(lines, ["^.+\\.pkg\\.dev$", "^registry\\.npmjs\\.org$"])
        self.assertEqual(json.loads(json.dumps(report.to_json()))["refused"][0]["rule"]["port"], 443)

    def test_patterns(self) -> None:
        """A host matches only itself; a wildcard, its subdomains, at any depth."""
        exact = re.compile(gateway_squid.pattern("registry.npmjs.org"))
        self.assertTrue(exact.search("registry.npmjs.org"))
        for other in ("x.registry.npmjs.org", "registryXnpmjs.org", "registry.npmjs.org.evil.com"):
            with self.subTest(other=other):
                self.assertIsNone(exact.search(other), other)
        wild = re.compile(gateway_squid.pattern("*.pkg.dev"))
        for match in ("us.pkg.dev", "a.b.pkg.dev"):
            with self.subTest(match=match):
                self.assertTrue(wild.search(match), match)
        for other in ("pkg.dev", "evilpkg.dev", "pkg.dev.evil.com", "us.pkgXdev"):
            with self.subTest(other=other):
                self.assertIsNone(wild.search(other), other)

    def test_nothing_accepted(self) -> None:
        """No rules, no hosts: nothing gets out."""
        _, text = gateway_squid.translate([])
        self.assertEqual([l for l in text.splitlines() if not l.startswith("#")], [])


class ScriptTest(unittest.TestCase):
    """Test gateway_squid.py."""

    def run_script(self, suc: object) -> tuple[int, dict[str, object], Path]:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        universal = directory / "suc.json"
        universal.write_text(json.dumps(suc), encoding="utf-8")
        output = directory / "out.txt"
        res = subprocess.run(  # nosec: B603  # Our own script, our own arguments.
            [sys.executable, str(SCRIPT), str(universal), str(output)],
            capture_output=True, check=False,
            cwd=SCRIPT.parent, env={**os.environ, "PYTHONPATH": str(SCRIPT.parent)},
        )
        report = json.loads(res.stdout) if res.stdout else {}
        return res.returncode, report, output

    def test_writes(self) -> None:
        """Every rule enforced: the list is written, readable by the gateway's user."""
        code, report, output = self.run_script({"egress": {"rules": [_rule("a.example.com")]}})
        self.assertEqual(code, 0)
        self.assertEqual(report["refused"], [])
        self.assertIn("^a\\.example\\.com$", output.read_text(encoding="utf-8"))
        if not fileutil.is_windows():
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o644)

    def test_refuses(self) -> None:
        """A rule it can't enforce: nothing written, exit 1."""
        code, report, output = self.run_script({"egress": {"rules": [_rule("a.example.com", port=22)]}})
        self.assertEqual(code, 1)
        self.assertEqual(len(report["refused"]), 1)  # type: ignore[arg-type]
        self.assertFalse(output.exists())

    def test_bad_input(self) -> None:
        """A bad rule, or a bad file: exit 2."""
        code, _, output = self.run_script({"egress": {"rules": [_rule("*")]}})
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        code, _, _ = self.run_script([1])
        self.assertEqual(code, 2)
