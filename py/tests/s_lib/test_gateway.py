#SPDX:MIT
"""Test the gateway module."""
import unittest

from surf_scoter_lib import gateway
from surf_scoter_lib.user_config import UserConfig


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


class SecretsTest(unittest.TestCase):
    """Test a rule's secrets."""

    def test_secrets(self) -> None:
        """An `https` rule on an exact host may carry secret names; they're kept, in order."""
        rules = gateway.rules_of([_rule("api.github.com", "https", secrets=["GITHUB_TOKEN", "B_2"])])
        self.assertEqual(rules[0].secrets, ("GITHUB_TOKEN", "B_2"))
        self.assertEqual(rules[0].to_json()["secrets"], ["GITHUB_TOKEN", "B_2"])
        self.assertEqual(gateway.rules_of([_rule("a.example.com")])[0].secrets, ())

    def test_bad_secrets(self) -> None:
        """What the generator refuses is refused here too."""
        for bad in (
            _rule("a.example.com", "tls", secrets=["A"]),
            _rule("a.example.com", "http", secrets=["A"]),
            _rule("*.example.com", "https", secrets=["A"]),
            _rule("a.example.com", "https", secrets=["lower"]),
            _rule("a.example.com", "https", secrets=["A", "A"]),
            _rule("a.example.com", "https", secrets=["A" * 65]),
            _rule("a.example.com", "https", secrets="A"),
        ):
            with self.subTest(bad=bad), self.assertRaises(gateway.RuleErr):
                gateway.rules_of([bad])


class SetupPortsTest(unittest.TestCase):
    """Test reading the setup's gateway port lists."""

    @staticmethod
    def _config(ports: object) -> UserConfig:
        return UserConfig({"resolved": {"dev-env": {"value-lists": {gateway.GATEWAY_PORTS: ports}}}})

    def test_ports(self) -> None:
        """Each entry a port, as text; repeats collapse."""
        self.assertEqual(
            gateway.setup_ports(self._config(["443", "8443", "443"]), gateway.GATEWAY_PORTS),
            frozenset({443, 8443}),
        )
        self.assertEqual(gateway.ports_text(frozenset({8443, 443})), "443, 8443")

    def test_unset_is_no_ports(self) -> None:
        """Not set, or empty: no ports, so nothing is let through."""
        self.assertEqual(gateway.setup_ports(UserConfig({}), gateway.GATEWAY_TLS_PORTS), frozenset())
        self.assertEqual(gateway.setup_ports(self._config([]), gateway.GATEWAY_PORTS), frozenset())
        self.assertEqual(gateway.ports_text(frozenset()), "none")

    def test_bad_entries(self) -> None:
        """Anything but a TCP port is an error, not skipped."""
        for bad in (["0"], ["65536"], ["-1"], ["https"], ["４４３"], [" 443"], "443", [443]):
            with self.subTest(bad=bad), self.assertRaises(gateway.RuleErr):
                gateway.setup_ports(self._config(bad), gateway.GATEWAY_PORTS)
