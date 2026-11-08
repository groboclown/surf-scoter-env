#SPDX:MIT
"""Test reading the network gateway's log (`scoter netlog`)."""
import datetime
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
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
