#SPDX:MIT
"""Test the gateway_seekrit script."""
import json
import os
import shutil
import subprocess  # nosec: B404  # Runs the gateway script under test.
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

# The scripts import the library as their own directory's, as they do when
# they run.
SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
import gateway_seekrit
from surf_scoter_lib import fileutil

SCRIPT = SCRIPTS / "gateway_seekrit.py"
PORTS = frozenset({443})
TLS_PORTS = frozenset({443})


def _rule(host: str, protocol: str = "tls", **extra: object) -> dict[str, object]:
    return {"host": host, "protocol": protocol, **extra}


def _suc(
    *rules: dict[str, object], ports: list[str] | None = None, tls_ports: list[str] | None = None,
) -> dict[str, object]:
    """A universal configuration file with these rules, and the setup's gateway ports."""
    return {
        "egress": {"rules": list(rules)},
        "resolved": {"dev-env": {"value-lists": {
            "network-gateway-ports": ["443"] if ports is None else ports,
            "network-gateway-tls-ports": ["443"] if tls_ports is None else tls_ports,
        }}},
    }


def _translate(
    *rules: dict[str, object], ports: frozenset[int] = PORTS, tls_ports: frozenset[int] = TLS_PORTS,
) -> tuple[dict[str, str], dict[str, object], list[str]]:
    """(refusal reasons by host, the configuration as parsed, enforced hosts)."""
    parsed = gateway_seekrit.gateway.rules_from(gateway_seekrit.UserConfig(_suc(*rules)))
    report, text = gateway_seekrit.translate(parsed, ports, tls_ports)
    reasons = {rule.host: reason for rule, reason in report.refused}
    return reasons, tomllib.loads(text), [rule.host for rule in report.enforced]


def _hosts(config: dict[str, object]) -> list[dict[str, object]]:
    return config["forward"].get("host", [])  # type: ignore[union-attr, no-any-return]


class TranslateTest(unittest.TestCase):
    """Test the translation into seekrit-proxy's configuration."""

    def test_fixed_settings(self) -> None:
        """Deny the rest, no secrets over plain HTTP, secrets injected after start, redaction."""
        _, config, _ = _translate()
        forward = config["forward"]
        self.assertEqual(forward["listen"], "0.0.0.0:3128")  # type: ignore[index]
        self.assertEqual(forward["unmatched_host_policy"], "deny")  # type: ignore[index]
        self.assertEqual(forward["plaintext_injection"], "refuse")  # type: ignore[index]
        self.assertEqual(forward["ca_cert"], gateway_seekrit.CA_CERT)  # type: ignore[index]
        self.assertEqual(forward["ca_key"], gateway_seekrit.CA_KEY)  # type: ignore[index]
        self.assertEqual(config["secrets"], {"source": "inject", "socket": gateway_seekrit.INJECT_SOCKET})
        self.assertEqual(config["redaction"], {"enabled": True})
        self.assertEqual(config["egress"], {"block_private_ips": True})
        self.assertNotIn("route", config)
        self.assertEqual(_hosts(config), [])

    def test_tls_is_a_tunnel(self) -> None:
        """`tls` on an allowed port: passed through untouched, only on its ports."""
        _, config, enforced = _translate(_rule("pypi.org"))
        self.assertEqual(enforced, ["pypi.org"])
        self.assertEqual(_hosts(config), [{"match": "pypi.org", "intercept": False, "ports": [443]}])

    def test_https_is_intercepted(self) -> None:
        """`https`: read, with its methods, and its secrets allowed."""
        _, config, _ = _translate(
            _rule("api.github.com", "https", methods=["GET"], secrets=["GITHUB_TOKEN"]),
        )
        self.assertEqual(_hosts(config), [{
            "match": "api.github.com", "ports": [443], "methods": ["GET"], "allow": ["GITHUB_TOKEN"],
        }])

    def test_http_never_gets_secrets(self) -> None:
        """`http`: read, with its methods, never an allow list."""
        _, config, _ = _translate(
            _rule("mirror.example.com", "http", port=80, methods=["GET"]),
            ports=frozenset({80, 443}),
        )
        self.assertEqual(_hosts(config), [{"match": "mirror.example.com", "ports": [80], "methods": ["GET"]}])

    def test_wildcards_refused(self) -> None:
        """The gateway matches names exactly, so a wildcard is refused, whatever the protocol."""
        reasons, config, _ = _translate(_rule("*.pkg.dev"), _rule("*.example.com", "https"))
        self.assertIn("doesn't allow wildcard hosts", reasons["*.pkg.dev"])
        self.assertIn("doesn't allow wildcard hosts", reasons["*.example.com"])
        self.assertEqual(_hosts(config), [])

    def test_ports(self) -> None:
        """A port the firewall doesn't let out is refused; `https` also needs a TLS port."""
        reasons, _, _ = _translate(_rule("git.example.com", port=22))
        self.assertIn("only lets TCP 443 out, not 22", reasons["git.example.com"])
        reasons, _, enforced = _translate(
            _rule("a.example.com", port=8443),
            _rule("b.example.com", "https", port=8443),
            ports=frozenset({443, 8443}),
        )
        self.assertEqual(enforced, ["a.example.com"])
        self.assertIn("only looks inside TLS on TCP 443, not 8443", reasons["b.example.com"])
        reasons, _, _ = _translate(_rule("a.example.com"), ports=frozenset())
        self.assertIn("only lets TCP none out", reasons["a.example.com"])

    def test_one_host_tunnelled_and_read_refused(self) -> None:
        """A host is either read or passed through: `tls` with `https` on one host is ambiguous."""
        reasons, config, enforced = _translate(
            _rule("api.example.com"), _rule("api.example.com", "https", methods=["GET"]),
            _rule("other.example.com"),
        )
        self.assertEqual(enforced, ["other.example.com"])
        self.assertIn("reads a host's traffic or passes it through", reasons["api.example.com"])
        self.assertEqual([h["match"] for h in _hosts(config)], ["other.example.com"])

    def test_one_host_http_and_https_refused(self) -> None:
        """One rule set serves both, so `https` and `http` for one host are refused."""
        reasons, _, _ = _translate(
            _rule("a.example.com", "https"), _rule("a.example.com", "http", port=443, methods=["GET"]),
        )
        self.assertIn("can't tell a host's `https` rules from its `http` ones", reasons["a.example.com"])

    def test_one_host_reading_on_two_ports_refused(self) -> None:
        """Ports are checked before a request is read, so a host's read rules share one port."""
        reasons, _, _ = _translate(
            _rule("a.example.com", "https", methods=["GET"]),
            _rule("a.example.com", "https", port=8443, methods=["POST"]),
            ports=frozenset({443, 8443}), tls_ports=frozenset({443, 8443}),
        )
        self.assertIn("must all be on one port", reasons["a.example.com"])

    def test_tunnel_on_several_ports(self) -> None:
        """A host's `tls` rules become one tunnel on all their ports: no methods to mix up."""
        _, config, _ = _translate(
            _rule("a.example.com"), _rule("a.example.com", port=8443), ports=frozenset({443, 8443}),
        )
        self.assertEqual(_hosts(config), [{"match": "a.example.com", "intercept": False, "ports": [443, 8443]}])

    def test_rules_for_one_host(self) -> None:
        """Rules with the same methods merge, with all their secrets; narrower ones come first."""
        _, config, _ = _translate(
            _rule("api.example.com", "https", secrets=["ANY_KEY"]),
            _rule("api.example.com", "https", methods=["HEAD", "GET"], secrets=["READ_KEY"]),
            _rule("api.example.com", "https", methods=["GET", "HEAD"], secrets=["READ_KEY", "OTHER"]),
        )
        self.assertEqual(_hosts(config), [
            {"match": "api.example.com", "ports": [443], "methods": ["GET", "HEAD"], "allow": ["READ_KEY", "OTHER"]},
            {"match": "api.example.com", "ports": [443], "allow": ["ANY_KEY"]},
        ])

    def test_text_is_quoted(self) -> None:
        """Every value is a quoted TOML string: nothing in a rule can add a key."""
        text = gateway_seekrit.config_text([gateway_seekrit.HostRule('a"b\n[x]', True, [443])])
        config = tomllib.loads(text)
        self.assertEqual(_hosts(config)[0]["match"], 'a"b\n[x]')
        self.assertNotIn("x", config)


class ScriptTest(unittest.TestCase):
    """Test gateway_seekrit.py."""

    def run_script(self, suc: object) -> tuple[int, dict[str, object], Path]:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        universal = directory / "suc.json"
        universal.write_text(json.dumps(suc), encoding="utf-8")
        output = directory / "seekrit-proxy.toml"
        res = subprocess.run(  # nosec: B603  # Our own script, our own arguments.
            [sys.executable, str(SCRIPT), str(universal), str(output)],
            capture_output=True, check=False,
            cwd=SCRIPT.parent, env={**os.environ, "PYTHONPATH": str(SCRIPT.parent)},
        )
        report = json.loads(res.stdout) if res.stdout else {}
        return res.returncode, report, output

    def test_writes(self) -> None:
        """Every rule enforced: the configuration is written, readable by the gateway's user."""
        code, report, output = self.run_script(_suc(
            _rule("pypi.org"), _rule("api.github.com", "https", secrets=["GITHUB_TOKEN"]),
        ))
        self.assertEqual(code, 0)
        self.assertEqual(report["gateway"], "seekrit")
        self.assertEqual(report["refused"], [])
        config = tomllib.loads(output.read_text(encoding="utf-8"))
        self.assertEqual([h["match"] for h in _hosts(config)], ["pypi.org", "api.github.com"])
        if not fileutil.is_windows():
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o644)

    def test_refuses(self) -> None:
        """A rule it can't enforce: nothing written, exit 1."""
        code, report, output = self.run_script(_suc(_rule("*.pkg.dev")))
        self.assertEqual(code, 1)
        self.assertEqual(len(report["refused"]), 1)  # type: ignore[arg-type]
        self.assertFalse(output.exists())

    def test_bad_input(self) -> None:
        """A bad rule, a bad port list, or a bad file: exit 2."""
        code, _, output = self.run_script(_suc(_rule("a.example.com", "tls", secrets=["A"])))
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        code, _, output = self.run_script(_suc(_rule("a.example.com"), tls_ports=["x"]))
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        code, _, _ = self.run_script([1])
        self.assertEqual(code, 2)
