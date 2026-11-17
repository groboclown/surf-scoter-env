#SPDX:MIT
"""Test the gateway_envoy script."""
import json
import os
import re
import shutil
import subprocess  # nosec: B404  # Runs the gateway script and openssl under test.
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

import gateway_envoy
from surf_scoter_lib import UserConfig, fileutil, gateway

from . import REPO, SRC_BIN

SCRIPT = SRC_BIN / "gateway_envoy.py"
DEFAULT_CONFIG = REPO / "containers" / "network-gateway-envoy" / "envoy.json"
OPENSSL = shutil.which("openssl")

# The distribution's `network-gateway-mk-leaf`, with `${openssl}` resolved.
MK_LEAF = [
    "openssl", "req", "-x509", "-CA", "{x:ca-cert}", "-CAkey", "{x:ca-key}",
    "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
    "-keyout", "{x:leaf-key}", "-out", "{x:leaf-cert}", "-days", "30",
    "-subj", "{x:subject}", "-addext", "{x:san}",
    "-addext", "basicConstraints=critical,CA:FALSE",
    "-addext", "keyUsage=critical,digitalSignature",
    "-addext", "extendedKeyUsage=serverAuth",
]
FAKE_LEAF = ("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
             "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")


def _rule(host: str, protocol: str = "tls", **extra: object) -> dict[str, object]:
    return {"host": host, "protocol": protocol, **extra}


def _suc(*rules: dict[str, object], ports: list[str] | None = None, tls_ports: list[str] | None = None) -> dict[str, object]:
    """A universal configuration file with these rules, and the setup's gateway ports and leaf maker."""
    return {
        "egress": {"rules": list(rules)},
        "resolved": {"dev-env": {
            "value-lists": {
                "network-gateway-ports": ["443"] if ports is None else ports,
                "network-gateway-tls-ports": ["443"] if tls_ports is None else tls_ports,
            },
            "runs": {"network-gateway-mk-leaf": MK_LEAF},
        }},
    }


def _rules(*rules: dict[str, object]) -> list[gateway.Rule]:
    return gateway.rules_of(list(rules))


PORTS = frozenset({443})


def _plan(*rules: dict[str, object], ports: frozenset[int] = PORTS) -> gateway.Report:
    return gateway_envoy.plan(_rules(*rules), ports, PORTS)


def _config(*rules: dict[str, object]) -> dict[str, object]:
    enforced = _rules(*rules)
    leaves = {r.host: FAKE_LEAF for r in enforced if r.protocol == "https"}
    return gateway_envoy.config(enforced, leaves)


def _walk(value: object) -> Iterator[tuple[str, object]]:
    """Every key in the configuration, at any depth, with its value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _listeners(config: dict[str, object]) -> list[dict[str, object]]:
    return config["static_resources"]["listeners"]  # type: ignore[index,no-any-return]


def _clusters(config: dict[str, object]) -> dict[str, dict[str, object]]:
    return {c["name"]: c for c in config["static_resources"]["clusters"]}  # type: ignore[index]


def _manager(listener: dict[str, object]) -> dict[str, object]:
    return listener["filter_chains"][0]["filters"][0]["typed_config"]  # type: ignore[index,no-any-return]


def _routes(config: dict[str, object], listener: int = 0) -> list[dict[str, object]]:
    manager = _manager(_listeners(config)[listener])
    return manager["route_config"]["virtual_hosts"][0]["routes"]  # type: ignore[index,no-any-return]


def _header_matches(header: dict[str, object], value: str) -> bool:
    """Whether a header matcher takes `value`, as Envoy would."""
    matcher = header["string_match"]
    if "exact" in matcher:  # type: ignore[operator]
        return value.lower() == matcher["exact"].lower()  # type: ignore[index,no-any-return]
    # RE2's syntax, as used here, is Python's too; safe_regex must match the whole value.
    return re.fullmatch(matcher["safe_regex"]["regex"], value) is not None  # type: ignore[index]


def _matches(route: dict[str, object], authority: str) -> bool:
    return _header_matches(route["match"]["headers"][0], authority)  # type: ignore[index]


class PlanTest(unittest.TestCase):
    """Test what the gateway enforces, and what it refuses."""

    def test_enforced_and_refused(self) -> None:
        """`tls` and `https` on the firewall's ports; everything else refused, with its reason."""
        report = _plan(
            _rule("registry.npmjs.org"),
            _rule("*.pkg.dev"),
            _rule("a.example.com", protocol="https", methods=["GET"]),
            _rule("b.example.com", protocol="http", methods=["GET"]),
            _rule("git.example.com", port=22),
            _rule("api.github.com", protocol="https", secrets=["GITHUB_TOKEN"]),
            _rule("*.read.example.com", protocol="https"),
            _rule("c.example.com", methods=["GET"]),
        )
        self.assertEqual(report.gateway, "envoy")
        self.assertEqual([r.host for r in report.enforced], ["registry.npmjs.org", "*.pkg.dev", "a.example.com"])
        reasons = {rule.host: reason for rule, reason in report.refused}
        self.assertIn("never plain HTTP", reasons["b.example.com"])
        self.assertIn("only lets TCP 443 out, not 22", reasons["git.example.com"])
        self.assertIn("can't fill in secrets", reasons["api.github.com"])
        self.assertIn("GITHUB_TOKEN", reasons["api.github.com"])
        self.assertIn("host named exactly", reasons["*.read.example.com"])
        self.assertIn("can't see the methods", reasons["c.example.com"])

    def test_tls_ports(self) -> None:
        """An `https` rule only on a port the gateway reads TLS on; a `tls` rule on any the firewall lets out."""
        rules = _rules(_rule("a.example.com", port=8443), _rule("b.example.com", protocol="https", port=8443))
        report = gateway_envoy.plan(rules, frozenset({443, 8443}), frozenset({443}))
        self.assertEqual([r.host for r in report.enforced], ["a.example.com"])
        self.assertIn("only looks inside TLS on TCP 443, not 8443", report.refused[0][1])
        report = gateway_envoy.plan(rules, frozenset(), frozenset({443, 8443}))
        self.assertEqual(report.enforced, [])
        self.assertIn("only lets TCP none out", report.refused[0][1])

    def test_read_or_pass_through(self) -> None:
        """A host and port is read or passed through, never both: a `tls` rule, exact or a
        wildcard, that takes an `https` rule's host and port refuses the `https` rule."""
        report = _plan(
            _rule("a.example.com"), _rule("a.example.com", protocol="https"),
            _rule("*.pkg.dev"), _rule("us.pkg.dev", protocol="https"),
            _rule("b.example.com", port=8443), _rule("b.example.com", protocol="https"),
            _rule("pkg.dev", protocol="https"),
            ports=frozenset({443, 8443}),
        )
        refused = [(rule.host, rule.protocol) for rule, _ in report.refused]
        self.assertEqual(refused, [("a.example.com", "https"), ("us.pkg.dev", "https")])
        self.assertIn("passes us.pkg.dev:443 through", report.refused[1][1])
        enforced = [(r.host, r.protocol, r.port) for r in report.enforced]
        self.assertIn(("b.example.com", "https", 443), enforced)
        self.assertIn(("pkg.dev", "https", 443), enforced)

    def test_methods_together(self) -> None:
        """A read host's rules allow the methods of all of them; one without methods allows any."""
        rules = _rules(
            _rule("a.example.com", protocol="https", methods=["GET"]),
            _rule("a.example.com", protocol="https", methods=["HEAD", "GET"]),
            _rule("b.example.com", protocol="https", methods=["GET"]),
            _rule("b.example.com", protocol="https"),
        )
        self.assertEqual(gateway_envoy.read_hosts(rules), {
            ("a.example.com", 443): frozenset({"GET", "HEAD"}),
            ("b.example.com", 443): None,
        })


class ConfigTest(unittest.TestCase):
    """Test the configuration's routes."""

    def test_connect_routes(self) -> None:
        """Read hosts' `CONNECT`s first, to their own listener; then the tunnels, once each."""
        config = _config(
            _rule("a.example.com", port=8443), _rule("a.example.com", port=8443),
            _rule("b.example.com", protocol="https"), _rule("*.pkg.dev"),
        )
        routes = _routes(config)
        self.assertEqual([r["name"] for r in routes], [
            "intercept b.example.com:443", "a.example.com:8443", "*.pkg.dev:443",
        ])
        self.assertEqual(routes[0]["route"]["cluster"], "intercept b.example.com:443")  # type: ignore[index]
        self.assertEqual(routes[1]["route"]["cluster"], gateway_envoy.CLUSTER)  # type: ignore[index]

    def test_exact_authority(self) -> None:
        """A host matches only itself, on its port, in any case."""
        route = _routes(_config(_rule("registry.npmjs.org")))[0]
        self.assertTrue(_matches(route, "registry.npmjs.org:443"))
        self.assertTrue(_matches(route, "REGISTRY.npmjs.org:443"))
        for other in (
            "registry.npmjs.org", "registry.npmjs.org:4430", "registry.npmjs.org:8443",
            "registry.npmjs.org.:443", "x.registry.npmjs.org:443", "registry.npmjs.org.evil.com:443",
            "user@registry.npmjs.org:443", "registry.npmjs.org:443@evil.com:443",
        ):
            with self.subTest(other=other):
                self.assertFalse(_matches(route, other), other)

    def test_wildcard_authority(self) -> None:
        """A wildcard matches its subdomains, at any depth, on its port; never the domain itself."""
        route = _routes(_config(_rule("*.pkg.dev")))[0]
        for match in ("us.pkg.dev:443", "a.b.pkg.dev:443", "US-East.PKG.dev:443"):
            with self.subTest(match=match):
                self.assertTrue(_matches(route, match), match)
        for other in (
            "pkg.dev:443", "evilpkg.dev:443", "pkg.dev.evil.com:443", "us.pkgXdev:443",
            "us.pkg.dev", "us.pkg.dev:4430", "us.pkg.dev:44", "us.pkg.dev.:443",
            "evil.com#.pkg.dev:443", "user@us.pkg.dev:443", "evil.com:443/.pkg.dev:443",
            ".pkg.dev:443", "a..pkg.dev:443", "us.pkg.dev:443\n",
        ):
            with self.subTest(other=other):
                self.assertFalse(_matches(route, other), other)

    def test_read_host_requests(self) -> None:
        """A read host's requests: only for that host (its port optional on 443), only with
        the rules' methods; anything else refused with a 403, never sent on."""
        config = _config(_rule("api.github.com", protocol="https", methods=["GET", "HEAD"]))
        allowed, refused = _routes(config, listener=1)
        authority, method = allowed["match"]["headers"]  # type: ignore[index]
        for match in ("api.github.com", "API.github.com", "api.github.com:443"):
            with self.subTest(match=match):
                self.assertTrue(_header_matches(authority, match))
        for other in (
            "registry.npmjs.org", "api.github.com:8443", "api.github.com.", "x.api.github.com",
            "api.github.com.evil.com", "user@api.github.com", "",
        ):
            with self.subTest(other=other):
                self.assertFalse(_header_matches(authority, other))
        self.assertTrue(_header_matches(method, "GET"))
        self.assertTrue(_header_matches(method, "HEAD"))
        for other in ("POST", "GETX", "get", "GET HEAD", "XGET"):
            with self.subTest(other=other):
                self.assertFalse(_header_matches(method, other))
        self.assertEqual(allowed["route"], {"cluster": "upstream api.github.com:443", "timeout": "0s"})
        self.assertEqual(refused["name"], "refused")
        self.assertEqual(refused["direct_response"]["status"], 403)  # type: ignore[index]
        self.assertNotIn("route", refused)

    def test_read_host_other_port(self) -> None:
        """On a port that isn't 443, the request names it."""
        regex = gateway_envoy.request_authority_regex("a.example.com", 8443)
        self.assertTrue(re.fullmatch(regex, "a.example.com:8443"))
        self.assertFalse(re.fullmatch(regex, "a.example.com"))

    def test_any_method(self) -> None:
        """A rule without methods: no method matcher."""
        allowed, _ = _routes(_config(_rule("a.example.com", protocol="https")), listener=1)
        self.assertEqual([h["name"] for h in allowed["match"]["headers"]], [":authority"])  # type: ignore[index]

    def test_needs_leaves(self) -> None:
        with self.assertRaises(ValueError):
            gateway_envoy.config(_rules(_rule("a.example.com", protocol="https")), {})

    def test_nothing_accepted(self) -> None:
        """No rules, no routes, no other listeners: nothing gets out."""
        config = gateway_envoy.config([])
        self.assertEqual(_routes(config), [])
        self.assertEqual(len(_listeners(config)), 1)
        self.assertNotIn("bootstrap_extensions", config)


class InvariantTest(unittest.TestCase):
    """What keeps the gateway from being a confused deputy (see the module's
    docstring).  A change that breaks one of these needs a security review,
    not a test update."""

    def setUp(self) -> None:
        self.config = _config(
            _rule("registry.npmjs.org"), _rule("*.pkg.dev"), _rule("a.example.com", port=8443),
            _rule("api.github.com", protocol="https", methods=["GET"]),
            _rule("b.example.com", protocol="https"),
        )

    def test_nothing_redirects_the_host(self) -> None:
        """Nothing can make the gateway dial anything but the host the route matched."""
        forbidden = {
            "host_rewrite_literal", "host_rewrite_header", "host_rewrite_path_regex",
            "auto_host_rewrite", "allow_dynamic_host_from_filter_state",
            "strip_any_host_port", "strip_matching_host_port", "strip_trailing_host_dot",
            "save_upstream_address", "sub_clusters_config", "original_dst_lb_config",
            "passthrough_filter_state_objects",
        }
        found = {key for key, _ in _walk(self.config)} & forbidden
        self.assertEqual(found, set())

    def test_only_these_filters(self) -> None:
        """The `CONNECT` listener's HTTP filters are the dynamic forward proxy, then the router;
        a read host's, the router only.  Nothing sets filter state."""
        listeners = _listeners(self.config)
        names = [f["name"] for f in _manager(listeners[0])["http_filters"]]  # type: ignore[attr-defined]
        self.assertEqual(names, ["envoy.filters.http.dynamic_forward_proxy", "envoy.filters.http.router"])
        for listener in listeners[1:]:
            with self.subTest(listener=listener["name"]):
                names = [f["name"] for f in _manager(listener)["http_filters"]]  # type: ignore[attr-defined]
                self.assertEqual(names, ["envoy.filters.http.router"])
        for listener in listeners:
            self.assertNotIn("listener_filters", listener)
            self.assertEqual(len(listener["filter_chains"]), 1)  # type: ignore[arg-type]
            self.assertEqual(len(listener["filter_chains"][0]["filters"]), 1)  # type: ignore[index]

    def test_no_admin_one_socket(self) -> None:
        """No admin interface for a loopback name to reach; one listening socket, the
        others internal."""
        self.assertNotIn("admin", self.config)
        self.assertNotIn("dynamic_resources", self.config)
        listeners = _listeners(self.config)
        self.assertIn("address", listeners[0])
        self.assertEqual(len(listeners), 3)
        for listener in listeners[1:]:
            self.assertEqual(listener["internal_listener"], {})
            self.assertNotIn("address", listener)

    def test_tunnel_cluster(self) -> None:
        """The dynamic forward proxy's cluster opens plain TCP: no TLS of its own, no shared connections."""
        cluster = _clusters(self.config)[gateway_envoy.CLUSTER]
        self.assertNotIn("transport_socket", cluster)
        self.assertNotIn("transport_socket_matches", cluster)
        self.assertNotIn("allow_coalesced_connections", {key for key, _ in _walk(self.config)})
        self.assertEqual(cluster["cluster_type"]["name"], "envoy.clusters.dynamic_forward_proxy")  # type: ignore[index]

    def test_read_host_upstreams(self) -> None:
        """Each read host's requests go to that host, fixed, over TLS that checks its name and chain."""
        clusters = _clusters(self.config)
        for host in ("api.github.com", "b.example.com"):
            with self.subTest(host=host):
                cluster = clusters[f"upstream {host}:443"]
                self.assertEqual(cluster["type"], "LOGICAL_DNS")
                (endpoint,) = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"]  # type: ignore[index]
                self.assertEqual(endpoint["endpoint"]["address"]["socket_address"], {"address": host, "port_value": 443})
                tls = cluster["transport_socket"]["typed_config"]  # type: ignore[index]
                self.assertEqual(tls["sni"], host)
                validation = tls["common_tls_context"]["validation_context"]
                self.assertEqual(validation["trusted_ca"], {"filename": gateway_envoy.TRUST_STORE})
                self.assertEqual(
                    validation["match_typed_subject_alt_names"],
                    [{"san_type": "DNS", "matcher": {"exact": host}}],
                )
                self.assertEqual(set(validation), {"trusted_ca", "match_typed_subject_alt_names"})
                inward = clusters[f"intercept {host}:443"]
                address = inward["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]  # type: ignore[index]
                self.assertEqual(address, {"envoy_internal_address": {"server_listener_name": f"intercept {host}:443"}})
        self.assertNotIn("trust_chain_verification", {key for key, _ in _walk(self.config)})

    def test_connect_routes(self) -> None:
        """Each `CONNECT` route takes `CONNECT` only, for a whole `:authority`, as a tunnel."""
        manager = _manager(_listeners(self.config)[0])
        self.assertEqual(manager["codec_type"], "HTTP1")
        vhosts = manager["route_config"]["virtual_hosts"]  # type: ignore[index]
        self.assertEqual(len(vhosts), 1)
        routes = vhosts[0]["routes"]
        self.assertEqual(len(routes), 5)
        for route in routes:
            with self.subTest(route=route["name"]):
                self.assertEqual(route["match"]["connect_matcher"], {})
                self.assertEqual(set(route["match"]), {"connect_matcher", "headers"})
                (header,) = route["match"]["headers"]
                self.assertEqual(header["name"], ":authority")
                self.assertEqual(set(route["route"]), {"cluster", "timeout", "upgrade_configs"})
                self.assertEqual(route["route"]["timeout"], "0s")
                self.assertEqual(
                    route["route"]["upgrade_configs"], [{"upgrade_type": "CONNECT", "connect_config": {}}],
                )

    def test_read_host_routes(self) -> None:
        """A read host's listener sends on only its own host's requests, to its own upstream."""
        for listener in _listeners(self.config)[1:]:
            name = listener["name"]
            host_port = name.removeprefix("intercept ")  # type: ignore[attr-defined]
            with self.subTest(listener=name):
                allowed, refused = _manager(listener)["route_config"]["virtual_hosts"][0]["routes"]  # type: ignore[index]
                self.assertEqual(allowed["match"]["headers"][0]["name"], ":authority")
                self.assertEqual(allowed["route"]["cluster"], f"upstream {host_port}")
                self.assertIn("direct_response", refused)

    def test_bounded_http2(self) -> None:
        """A read host's HTTP/2 windows are small, both ways: a slow reader can't fill the gateway."""
        windows = [value for key, value in _walk(self.config) if key == "http2_protocol_options"]
        self.assertEqual(len(windows), 4)
        for window in windows:
            self.assertLessEqual(window["initial_stream_window_size"], 1024 * 1024)  # type: ignore[index]
            self.assertLessEqual(window["initial_connection_window_size"], 4 * 1024 * 1024)  # type: ignore[index]

    def test_one_dns_cache(self) -> None:
        """The filter and the cluster share one DNS cache, IPv4 only, with limits."""
        caches = [value for key, value in _walk(self.config) if key == "dns_cache_config"]
        self.assertEqual(len(caches), 2)
        self.assertEqual(caches[0], caches[1])
        self.assertEqual(caches[0]["dns_lookup_family"], "V4_ONLY")  # type: ignore[index]
        self.assertIn("max_hosts", caches[0])  # type: ignore[operator]

    def test_default_config(self) -> None:
        """The image's own configuration is the one that allows nothing."""
        self.assertEqual(DEFAULT_CONFIG.read_text(encoding="utf-8"), gateway_envoy.config_text([]))


@unittest.skipIf(OPENSSL is None, "needs openssl")
class LeafTest(unittest.TestCase):
    """Test making the read hosts' certificates."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.ca_cert, self.ca_key = self.directory / "ca.pem", self.directory / "ca-key.pem"
        subprocess.run(  # nosec: B603  # openssl, with our own arguments.
            [str(OPENSSL), "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
             "-keyout", str(self.ca_key), "-out", str(self.ca_cert), "-days", "30", "-subj", "/CN=test CA",
             "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
            check=True, capture_output=True,
        )

    def test_signed_for_the_host(self) -> None:
        """Each certificate is for its host alone, signed by the CA, for a server."""
        config = UserConfig(_suc())
        leaves = gateway_envoy.make_leaves(config, ["api.github.com"], str(self.ca_cert), str(self.ca_key))
        cert, key = leaves["api.github.com"]
        self.assertIn("PRIVATE KEY", key)
        leaf = self.directory / "leaf.pem"
        leaf.write_text(cert, encoding="ascii")
        res = subprocess.run(  # nosec: B603  # openssl, with our own arguments.
            [str(OPENSSL), "verify", "-CAfile", str(self.ca_cert), "-purpose", "sslserver",
             "-verify_hostname", "api.github.com", str(leaf)],
            capture_output=True, check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        res = subprocess.run(  # nosec: B603  # openssl, with our own arguments.
            [str(OPENSSL), "verify", "-CAfile", str(self.ca_cert), "-verify_hostname", "evil.example.com", str(leaf)],
            capture_output=True, check=False,
        )
        self.assertNotEqual(res.returncode, 0)

    def test_refuses_wildcards(self) -> None:
        with self.assertRaises(gateway_envoy.SurfScoterErr):
            gateway_envoy.make_leaves(UserConfig(_suc()), ["*.example.com"], str(self.ca_cert), str(self.ca_key))

    def test_maker_fails(self) -> None:
        suc = _suc()
        suc["resolved"]["dev-env"]["runs"]["network-gateway-mk-leaf"] = ["false"]  # type: ignore[index]
        with self.assertRaises(gateway_envoy.SurfScoterErr):
            gateway_envoy.make_leaves(UserConfig(suc), ["a.example.com"], str(self.ca_cert), str(self.ca_key))


class ScriptTest(unittest.TestCase):
    """Test gateway_envoy.py."""

    def run_script(self, suc: object, *ca: str, private: bool = True) -> tuple[int, dict[str, object], Path, str]:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        if not private:
            os.chmod(directory, 0o755)  # nosec: B103  # Open, to test the refusal.
        universal = directory / "suc.json"
        universal.write_text(json.dumps(suc), encoding="utf-8")
        output = directory / "envoy.json"
        res = subprocess.run(  # nosec: B603  # Our own script, our own arguments.
            [sys.executable, str(SCRIPT), str(universal), str(output), *ca],
            capture_output=True, check=False,
            cwd=SCRIPT.parent, env={**os.environ, "PYTHONPATH": str(SCRIPT.parent)},
        )
        report = json.loads(res.stdout) if res.stdout else {}
        return res.returncode, report, output, res.stderr.decode("utf-8", "replace")

    def test_writes(self) -> None:
        """Every rule enforced: the configuration is written, readable by the gateway's user."""
        code, report, output, _ = self.run_script(_suc(_rule("a.example.com")))
        self.assertEqual(code, 0)
        self.assertEqual(report["refused"], [])
        config = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual([r["name"] for r in _routes(config)], ["a.example.com:443"])
        if not fileutil.is_windows():
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o644)

    @unittest.skipIf(OPENSSL is None, "needs openssl")
    def test_writes_read_hosts(self) -> None:
        """A read host: its certificate made from the run's CA, and in the configuration."""
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        ca_cert, ca_key = directory / "ca.pem", directory / "ca-key.pem"
        subprocess.run(  # nosec: B603  # openssl, with our own arguments.
            [str(OPENSSL), "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
             "-keyout", str(ca_key), "-out", str(ca_cert), "-days", "30", "-subj", "/CN=test CA",
             "-addext", "basicConstraints=critical,CA:TRUE"],
            check=True, capture_output=True,
        )
        suc = _suc(_rule("api.github.com", protocol="https", methods=["GET"]))
        code, _, output, stderr = self.run_script(suc, str(ca_cert), str(ca_key))
        self.assertEqual(code, 0, stderr)
        config = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual([r["name"] for r in _routes(config)], ["intercept api.github.com:443"])
        text = output.read_text(encoding="utf-8")
        self.assertIn("BEGIN CERTIFICATE", text)
        self.assertNotIn(ca_key.read_text(encoding="ascii").splitlines()[1], text)

    def test_read_hosts_need_the_ca(self) -> None:
        """A read host without the run's CA: nothing written, exit 2."""
        code, _, output, stderr = self.run_script(_suc(_rule("api.github.com", protocol="https")))
        self.assertEqual(code, 2)
        self.assertIn("needs the run's CA", stderr)
        self.assertFalse(output.exists())

    @unittest.skipIf(fileutil.is_windows(), "POSIX modes")
    def test_read_hosts_need_a_private_directory(self) -> None:
        """The certificate keys are readable by others: refused unless a directory above is yours alone."""
        if Path(tempfile.gettempdir()).stat().st_mode & 0o077 == 0:
            self.skipTest("the temporary directory is private")
        code, _, output, stderr = self.run_script(
            _suc(_rule("api.github.com", protocol="https")), "ca.pem", "ca-key.pem", private=False,
        )
        self.assertEqual(code, 2)
        self.assertIn("nothing above it is yours alone", stderr)
        self.assertFalse(output.exists())

    def test_no_ports_set(self) -> None:
        """A setup without the ports list: every rule refused, nothing written."""
        code, report, output, _ = self.run_script({"egress": {"rules": [_rule("a.example.com")]}})
        self.assertEqual(code, 1)
        self.assertEqual(len(report["refused"]), 1)  # type: ignore[arg-type]
        self.assertFalse(output.exists())

    def test_refuses(self) -> None:
        """A rule it can't enforce: nothing written, exit 1."""
        code, report, output, _ = self.run_script(_suc(_rule("a.example.com", port=22)))
        self.assertEqual(code, 1)
        self.assertEqual(len(report["refused"]), 1)  # type: ignore[arg-type]
        self.assertFalse(output.exists())

    def test_bad_input(self) -> None:
        """A bad rule, or a bad file: exit 2."""
        code, _, output, _ = self.run_script(_suc(_rule("*")))
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        code, _, output, _ = self.run_script(_suc(_rule("a.example.com"), ports=["https"]))
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        code, _, _, _ = self.run_script([1])
        self.assertEqual(code, 2)
