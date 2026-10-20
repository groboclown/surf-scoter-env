#SPDX:MIT
"""Test the gateway module."""
import json
import os
import re
import shutil
import subprocess  # nosec: B404  # Runs the gateway script under test.
import sys
import tempfile
import unittest
from pathlib import Path

from ...surf_scoter_lib import gateway
from ...surf_scoter_lib.user_config import UserConfig

SCRIPT = Path(__file__).parents[2] / "gateway_squid.py"


def _rule(host: str, protocol: str = "tls", **extra: object) -> dict[str, object]:
    return {"host": host, "protocol": protocol, **extra}


class RulesFromTest(unittest.TestCase):
    """Test reading the egress rules from the universal configuration file."""

    def test_accepted_first(self) -> None:
        """The accepted rules win over the project's; the project's are a stand-in."""
        suc = {
            "egress": {"gateway": "squid", "rules": [_rule("a.example.com", port=443, methods=[])]},
            "current-project-setup": {"egress": [_rule("b.example.com")]},
        }
        rules, source = gateway.rules_from(UserConfig(suc))
        self.assertEqual((source, [r.host for r in rules]), ("accepted", ["a.example.com"]))
        suc["egress"] = None
        rules, source = gateway.rules_from(UserConfig(suc))
        self.assertEqual((source, [r.host for r in rules]), ("project", ["b.example.com"]))
        # Ports default by protocol.
        self.assertEqual(rules[0].port, 443)
        self.assertEqual(gateway.rules_from(UserConfig({})), ([], "project"))

    def test_bad_rules(self) -> None:
        """Anything the generator wouldn't let through is refused, not passed on."""
        for bad in (
            _rule("a.example.com/x"),
            _rule("*"),
            _rule("*.com"),
            _rule("^evil$.example.com"),
            _rule("a.example.com", protocol="ssh"),
            _rule("a.example.com", port=0),
            _rule("a.example.com", port=True),
            _rule("a.example.com", protocol="http", methods=["get"]),
        ):
            with self.assertRaises(gateway.RuleErr, msg=str(bad)):
                gateway.rules_from(UserConfig({"egress": {"rules": [bad]}}))
        with self.assertRaises(gateway.RuleErr):
            gateway.rules_from(UserConfig({"egress": {"rules": "x"}}))


class SquidTest(unittest.TestCase):
    """Test the Squid gateway's translation."""

    def test_enforced_and_refused(self) -> None:
        """Only `tls` on 443; everything else refused, with its reason."""
        rules, _ = gateway.rules_from(UserConfig({"egress": {"rules": [
            _rule("registry.npmjs.org"),
            _rule("*.pkg.dev"),
            _rule("a.example.com", protocol="https", methods=["GET"]),
            _rule("b.example.com", protocol="http", methods=["GET"]),
            _rule("git.example.com", port=22),
        ]}}))
        report, text = gateway.squid(rules, "accepted")
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
        exact = re.compile(gateway.squid_pattern("registry.npmjs.org"))
        self.assertTrue(exact.search("registry.npmjs.org"))
        for other in ("x.registry.npmjs.org", "registryXnpmjs.org", "registry.npmjs.org.evil.com"):
            self.assertIsNone(exact.search(other), other)
        wild = re.compile(gateway.squid_pattern("*.pkg.dev"))
        for match in ("us.pkg.dev", "a.b.pkg.dev"):
            self.assertTrue(wild.search(match), match)
        for other in ("pkg.dev", "evilpkg.dev", "pkg.dev.evil.com", "us.pkgXdev"):
            self.assertIsNone(wild.search(other), other)

    def test_unreviewed_note(self) -> None:
        """The file says when its rules weren't reviewed."""
        _, text = gateway.squid([], "project")
        self.assertIn("unreviewed", text)
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
        if sys.platform != "win32":
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
