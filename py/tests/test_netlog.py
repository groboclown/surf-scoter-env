#SPDX:MIT
"""Test reading the network gateway's log (`scoter netlog`)."""
import datetime
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

import main_netlog
from surf_scoter_lib import UserConfig, netlog

# Squid's own lines, as its container logs them (startup messages too).
SQUID_LOG = """\
2026/11/08 06:13:14| Accepting HTTP Socket connections at conn3 local=[::]:3128 remote=[::] FD 11 flags=9
1794118406.582   8875 172.25.63.80 TCP_TUNNEL/200 48377821 CONNECT pypi.org:443 - HIER_DIRECT/151.101.192.223 -
1794118406.588      0 172.25.63.80 TCP_DENIED/403 3458 CONNECT example.com:443 - HIER_NONE/- text/html
1794118406.593      0 172.25.63.80 TCP_DENIED/403 3458 GET http://example.com/x? - HIER_NONE/- text/html
1794118406.597      0 172.25.63.80 TCP_DENIED/403 3442 CONNECT pypi.org:8443 - HIER_NONE/- text/html
"""

# seekrit-proxy's own lines (SEEKRIT_LOG_FORMAT=json), its other messages too.
SEEKRIT_LOG = """\
{"timestamp":"2026-11-08T02:08:33.455846Z","level":"INFO","message":"forward proxy (MITM) listening","secrets":0,"hosts":1,"tunnels":2,"listen":"0.0.0.0:3128","target":"seekrit_proxy"}
{"timestamp":"2026-11-08T02:08:35.892753Z","level":"INFO","message":"connect","event":"connect","host":"pypi.org","port":443,"action":"tunnel","reason":"tunnel_rule","target":"seekrit_access"}
{"timestamp":"2026-11-08T02:08:47.543748Z","level":"INFO","message":"connect","event":"connect","host":"example.com","port":443,"action":"deny","reason":"no_rule","target":"seekrit_access"}
{"timestamp":"2026-11-08T02:08:47.549908Z","level":"INFO","message":"connect","event":"connect","host":"api.github.com","port":443,"action":"intercept","reason":"rule","target":"seekrit_access"}
{"timestamp":"2026-11-08T02:10:04.241632Z","level":"INFO","message":"request","event":"request","plane":"intercept","method":"GET","host":"api.github.com","port":443,"path":"/user","status":401,"duration_ms":155,"injected":"GITHUB_TOKEN","target":"seekrit_access"}
{"timestamp":"2026-11-08T02:10:04.252024Z","level":"INFO","message":"request","event":"request","plane":"intercept","method":"POST","host":"api.github.com","port":443,"path":"/user","status":403,"duration_ms":0,"injected":"","target":"seekrit_access"}
{"timestamp":"2026-11-08T02:08:47.598298Z","level":"INFO","message":"tunnel closed","event":"tunnel_closed","host":"pypi.org","port":443,"bytes_up":2051,"bytes_down":48377805,"duration_ms":11705,"target":"seekrit_access"}
not json at all
{"timestamp":"2026-11-08T02:10:05Z","target":"seekrit_access","event":"connect","host":"x.example.com","port":443,"action":"something-else"}
"""


# The Envoy gateway's access log lines (gateway_envoy.py's format), as Envoy
# v1.36 wrote them: a tunnel, opened and ended; refusals of an unlisted host,
# a port no rule has, and a plain request; a malformed request; a host that
# didn't resolve; and a line of Envoy's own.
ENVOY_LOG = """\
[2026-11-17 02:16:21.805][1][warning][main] [source/server/server.cc:900] some message of Envoy's own
{"authority":"registry.npmjs.org:443","bytes_down":0,"bytes_up":0,"code":200,"flags":"-","method":"CONNECT","path":null,"route":"registry.npmjs.org:443","time":"2026-11-17T02:16:45.331Z","type":"DownstreamTunnelSuccessfullyEstablished","upstream":"104.16.4.34:443"}
{"authority":"registry.npmjs.org:443","bytes_down":3300,"bytes_up":863,"code":200,"flags":"DC","method":"CONNECT","path":null,"route":"registry.npmjs.org:443","time":"2026-11-17T02:16:45.331Z","type":"DownstreamEnd","upstream":"104.16.4.34:443"}
{"authority":"example.org:443","bytes_down":0,"bytes_up":0,"code":404,"flags":"NR","method":"CONNECT","path":null,"route":null,"time":"2026-11-17T02:16:47.615Z","type":"DownstreamEnd","upstream":null}
{"authority":"registry.npmjs.org:8443","bytes_down":0,"bytes_up":0,"code":404,"flags":"NR","method":"CONNECT","path":null,"route":null,"time":"2026-11-17T02:16:48.100Z","type":"DownstreamEnd","upstream":null}
{"authority":"Example.org","bytes_down":0,"bytes_up":0,"code":404,"flags":"NR","method":"GET","path":"/x","route":null,"time":"2026-11-17T02:16:48.676Z","type":"DownstreamEnd","upstream":null}
{"authority":null,"bytes_down":11,"bytes_up":0,"code":400,"flags":"DPE","method":null,"path":null,"route":null,"time":"2026-11-17T02:17:09.058Z","type":"DownstreamEnd","upstream":null}
{"authority":"gone.pkg.dev:443","bytes_down":0,"bytes_up":0,"code":503,"flags":"DF","method":"CONNECT","path":null,"route":"*.pkg.dev:443","time":"2026-11-17T02:17:10.000Z","type":"DownstreamEnd","upstream":null}
"""


class EnvoyTest(unittest.TestCase):
    """Test reading the Envoy gateway's JSON access log."""

    def test_events(self) -> None:
        """Tunnels opened and closed, with their bytes; refusals; nothing else."""
        events = netlog.parse(ENVOY_LOG.splitlines(), netlog.parse_envoy)
        self.assertEqual(
            [(e.action, e.host, e.port, e.method, e.reason) for e in events],
            [
                ("tunnel", "registry.npmjs.org", 443, None, None),
                ("closed", "registry.npmjs.org", 443, None, None),
                ("deny", "example.org", 443, None, "no_rule"),
                ("deny", "registry.npmjs.org", 8443, None, "no_rule"),
                ("deny", "example.org", 80, "GET", "no_rule"),
                ("closed", "gone.pkg.dev", 443, None, "DF"),
            ],
        )
        self.assertEqual((events[1].bytes_up, events[1].bytes_down), (863, 3300))
        self.assertEqual(events[4].path, "/x")
        self.assertEqual(events[0].time, datetime.datetime(2026, 11, 17, 2, 16, 45, 331000, datetime.UTC))

    def test_summary(self) -> None:
        """Hosts reached and refused; a rule suggested for each refused host, as for Squid."""
        summary = netlog.summarize(netlog.parse(ENVOY_LOG.splitlines(), netlog.parse_envoy))
        self.assertEqual(summary.suggestions(), [
            "{host: example.org, protocol: http}",
            "{host: example.org, protocol: tls}",
            "{host: registry.npmjs.org, protocol: tls, port: 8443}",
        ])
        self.assertIn("registry.npmjs.org:443", "\n".join(summary.lines()))

    def test_registered(self) -> None:
        self.assertIs(netlog.PARSERS["envoy"], netlog.parse_envoy)

    def test_read_host_secrets(self) -> None:
        """A read host with secrets: the names the filter put in, or why it refused."""
        events = netlog.parse(ENVOY_SECRETS_LOG.splitlines(), netlog.parse_envoy)
        self.assertEqual(
            [(e.status, e.reason, e.injected) for e in events],
            [
                (503, "secrets_pending", ()),
                (200, None, ("GITHUB_TOKEN",)),
                (403, "not_allowed", ()),
                (200, None, ()),
            ],
        )
        summary = netlog.summarize(events)
        self.assertEqual(dict(summary.injected), {"GITHUB_TOKEN": 1})  # nosec: B105  # A secret name and a count.

    def test_read_host(self) -> None:
        """A read host: its `CONNECT` is an intercept; each request, what it got, or its refusal.
        The host and port come from the gateway's own fields, not the request's `:authority`."""
        events = netlog.parse(ENVOY_READ_LOG.splitlines(), netlog.parse_envoy)
        self.assertEqual(
            [(e.action, e.host, e.port, e.method, e.path, e.status, e.reason) for e in events],
            [
                ("intercept", "api.github.com", 443, None, None, 200, None),
                ("request", "api.github.com", 443, "GET", "/user", 401, None),
                ("request", "api.github.com", 443, "POST", "/user", 403, "no_rule"),
                ("request", "api.github.com", 443, "GET", "/", 403, "no_rule"),
                ("closed", "api.github.com", 443, None, None, 200, None),
            ],
        )
        text = "\n".join(netlog.summarize(events).lines())
        self.assertIn("GET api.github.com/user -> 401", text)
        self.assertIn("POST api.github.com/user -> 403", text)
        self.assertIn("intercept", text)


# Envoy reading api.github.com (GET only): a GET, a POST refused, and a
# request naming another host on the same connection, refused.
ENVOY_READ_LOG = """\
{"authority":"api.github.com:443","bytes_down":0,"bytes_up":0,"code":200,"flags":"-","method":"CONNECT","path":null,"plane":"connect","route":"intercept api.github.com:443","time":"2026-11-17T02:35:02.000Z","type":"DownstreamTunnelSuccessfullyEstablished","upstream":"envoy://intercept api.github.com:443/"}
{"authority":"api.github.com","bytes_down":2396,"bytes_up":0,"code":401,"flags":"-","host":"api.github.com","method":"GET","path":"/user","plane":"request","port":"443","route":"api.github.com:443","time":"2026-11-17T02:35:02.102Z","type":"DownstreamEnd","upstream":"140.82.112.5:443"}
{"authority":"api.github.com","bytes_down":70,"bytes_up":0,"code":403,"flags":"-","host":"api.github.com","method":"POST","path":"/user","plane":"request","port":"443","route":"refused","time":"2026-11-17T02:35:03.105Z","type":"DownstreamEnd","upstream":null}
{"authority":"evil.example.com","bytes_down":70,"bytes_up":0,"code":403,"flags":"-","host":"api.github.com","method":"GET","path":"/","plane":"request","port":"443","route":"refused","time":"2026-11-17T02:35:03.508Z","type":"DownstreamEnd","upstream":null}
{"authority":"api.github.com:443","bytes_down":9000,"bytes_up":800,"code":200,"flags":"DC","method":"CONNECT","path":null,"plane":"connect","route":"intercept api.github.com:443","time":"2026-11-17T02:35:02.000Z","type":"DownstreamEnd","upstream":"envoy://intercept api.github.com:443/"}
"""

# Envoy reading api.github.com, whose rules name GITHUB_TOKEN: a request
# before the secrets arrived, one with the token filled in, one asking for a
# secret the rules don't name, and one without a placeholder.
ENVOY_SECRETS_LOG = """\
{"authority":"api.github.com","bytes_down":71,"bytes_up":0,"code":503,"flags":"-","host":"api.github.com","injected":null,"method":"GET","path":"/user","plane":"request","port":"443","refused":"secrets_pending","route":"api.github.com:443","time":"2026-11-17T02:35:01.000Z","type":"DownstreamEnd","upstream":null}
{"authority":"api.github.com","bytes_down":2396,"bytes_up":0,"code":200,"flags":"-","host":"api.github.com","injected":"GITHUB_TOKEN","method":"GET","path":"/user","plane":"request","port":"443","refused":null,"route":"api.github.com:443","time":"2026-11-17T02:35:02.000Z","type":"DownstreamEnd","upstream":"140.82.112.5:443"}
{"authority":"api.github.com","bytes_down":64,"bytes_up":0,"code":403,"flags":"-","host":"api.github.com","injected":null,"method":"GET","path":"/user","plane":"request","port":"443","refused":"not_allowed","route":"api.github.com:443","time":"2026-11-17T02:35:03.000Z","type":"DownstreamEnd","upstream":null}
{"authority":"api.github.com","bytes_down":10,"bytes_up":0,"code":200,"flags":"-","host":"api.github.com","injected":null,"method":"GET","path":"/","plane":"request","port":"443","refused":null,"route":"api.github.com:443","time":"2026-11-17T02:35:04.000Z","type":"DownstreamEnd","upstream":"140.82.112.5:443"}
"""


class SquidTest(unittest.TestCase):
    """Test reading Squid's access log."""

    def test_events(self) -> None:
        """Tunnels and refusals, with the host and port; its other lines skipped."""
        events = netlog.parse(SQUID_LOG.splitlines(), netlog.parse_squid)
        self.assertEqual(
            [(e.action, e.host, e.port, e.method, e.path) for e in events],
            [
                ("tunnel", "pypi.org", 443, None, None),
                ("deny", "example.com", 443, None, None),
                ("deny", "example.com", 80, "GET", "/x?"),
                ("deny", "pypi.org", 8443, None, None),
            ],
        )
        self.assertEqual(events[0].time, datetime.datetime.fromtimestamp(1794118406.582, datetime.UTC))

    def test_summary(self) -> None:
        """A refused host is suggested; one it reached on another port too, but not one it reached."""
        summary = netlog.summarize(netlog.parse(SQUID_LOG.splitlines(), netlog.parse_squid))
        self.assertEqual(summary.suggestions(), [
            "{host: example.com, protocol: http}",
            "{host: example.com, protocol: tls}",
            "{host: pypi.org, protocol: tls, port: 8443}",
        ])
        text = "\n".join(summary.lines())
        self.assertIn("pypi.org:443", text)
        self.assertNotIn("Requests the gateway read", text)


class SeekritTest(unittest.TestCase):
    """Test reading seekrit-proxy's JSON log."""

    def test_events(self) -> None:
        """Connections, requests with the secrets put in, and closed tunnels; nothing else."""
        events = netlog.parse(SEEKRIT_LOG.splitlines(), netlog.parse_seekrit)
        self.assertEqual(
            [(e.action, e.host, e.reason) for e in events],
            [
                ("tunnel", "pypi.org", "tunnel_rule"),
                ("deny", "example.com", "no_rule"),
                ("intercept", "api.github.com", "rule"),
                ("request", "api.github.com", None),
                ("request", "api.github.com", None),
                ("closed", "pypi.org", None),
            ],
        )
        get, post = events[3], events[4]
        self.assertEqual((get.method, get.path, get.status, get.injected), ("GET", "/user", 401, ("GITHUB_TOKEN",)))
        self.assertEqual((post.status, post.injected), (403, ()))
        self.assertEqual((events[5].bytes_up, events[5].bytes_down), (2051, 48377805))
        self.assertEqual(json.loads(json.dumps(get.to_json()))["injected"], ["GITHUB_TOKEN"])

    def test_summary(self) -> None:
        """Hosts, requests, and the secrets put in, by name; a rule suggested for the refused host."""
        summary = netlog.summarize(netlog.parse(SEEKRIT_LOG.splitlines(), netlog.parse_seekrit))
        text = "\n".join(summary.lines())
        self.assertIn("GET api.github.com/user -> 401  x1  with GITHUB_TOKEN", text)
        self.assertIn("POST api.github.com/user -> 403  x1", text)
        self.assertIn("example.com:443", text)
        self.assertIn("(no_rule)", text)
        self.assertIn("GITHUB_TOKEN", text.split("Secrets put into requests:")[1])
        self.assertEqual(summary.suggestions(), ["{host: example.com, protocol: tls}"])

    def test_nothing(self) -> None:
        self.assertEqual(netlog.summarize([]).lines(), ["Nothing in the gateway's log yet."])


class UntrustedTest(unittest.TestCase):
    """What's in the log came from the dev container."""

    def test_cleaned_and_never_suggested(self) -> None:
        """A host with terminal escapes is cleaned wherever it's shown, and no rule is suggested for it,
        nor for a wildcard."""
        evil = "evil.example.com\x1b]0;owned\x07"
        line = json.dumps({
            "timestamp": "2026-11-08T02:10:05Z", "target": "seekrit_access", "event": "connect",
            "host": evil, "port": 443, "action": "deny", "reason": "no_rule\x1b[2J",
        })
        star = json.dumps({
            "timestamp": "2026-11-08T02:10:06Z", "target": "seekrit_access", "event": "connect",
            "host": "*.example.com", "port": 443, "action": "deny", "reason": "no_rule",
        })
        events = netlog.parse([line, star], netlog.parse_seekrit)
        summary = netlog.summarize(events)
        for text in [*summary.lines(), *(e.line() for e in events)]:
            self.assertNotIn("\x1b", text)
            self.assertNotIn("\x07", text)
        self.assertEqual(summary.suggestions(), [])


class FollowTest(unittest.TestCase):
    """Test following by reading the log again."""

    def test_only_new_events(self) -> None:
        """Each read, only the lines the last didn't have, in the log's order, whatever their times."""
        lines = SEEKRIT_LOG.splitlines()
        reads = [lines[:3], lines[:5], lines[:5], lines]
        sleeps = iter(range(len(reads) - 1))

        def sleep(_: float) -> None:
            if next(sleeps, None) is None:
                raise KeyboardInterrupt

        out = io.StringIO()
        with (
            mock.patch.object(main_netlog, "_log_lines", side_effect=reads),
            mock.patch.object(main_netlog.time, "sleep", side_effect=sleep),
            redirect_stdout(out),
            self.assertRaises(KeyboardInterrupt),
        ):
            main_netlog._follow_poll(UserConfig({}), "c", netlog.parse_seekrit, True)
        shown = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([(e["action"], e["host"], e["method"]) for e in shown], [
            ("tunnel", "pypi.org", None), ("deny", "example.com", None),
            ("intercept", "api.github.com", None), ("request", "api.github.com", "GET"),
            ("request", "api.github.com", "POST"),
            # Logged with an earlier time than the line before it, and still shown.
            ("closed", "pypi.org", None),
        ])

    def test_gone(self) -> None:
        """A log that can't be read: it stops."""
        with mock.patch.object(main_netlog, "_log_lines", return_value=None):
            self.assertEqual(main_netlog._follow_poll(UserConfig({}), "c", netlog.parse_seekrit, False), 1)
