#SPDX:MIT
"""Test the gateway module."""
import unittest

from ...surf_scoter_lib import gateway
from ...surf_scoter_lib.user_config import UserConfig


def _rule(host: str, protocol: str = "tls", **extra: object) -> dict[str, object]:
    return {"host": host, "protocol": protocol, **extra}


class RulesFromTest(unittest.TestCase):
    """Test reading the egress rules from the universal configuration file."""

    def test_accepted_only(self) -> None:
        """Only the accepted rules; the project's are never used, so without any, none."""
        suc = {
            "egress": {"gateway": "squid", "rules": [_rule("a.example.com")]},
            "current-project-setup": {"egress": [_rule("b.example.com")]},
        }
        rules = gateway.rules_from(UserConfig(suc))
        self.assertEqual([r.host for r in rules], ["a.example.com"])
        # Ports default by protocol.
        self.assertEqual(rules[0].port, 443)
        suc["egress"] = None
        self.assertEqual(gateway.rules_from(UserConfig(suc)), [])
        self.assertEqual(gateway.rules_from(UserConfig({})), [])

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
            with self.subTest(bad=bad), self.assertRaises(gateway.RuleErr, msg=str(bad)):
                gateway.rules_from(UserConfig({"egress": {"rules": [bad]}}))
        with self.assertRaises(gateway.RuleErr):
            gateway.rules_from(UserConfig({"egress": {"rules": "x"}}))
