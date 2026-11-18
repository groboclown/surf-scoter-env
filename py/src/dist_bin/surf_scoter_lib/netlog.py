#SPDX:MIT
"""What the dev container did through the network gateway: its log, read.

Each gateway logs in its own way; a parser per gateway (by the setup's
`dev-env: values: network-gateway-name`) turns its lines into the same
events.  Squid sees only each `CONNECT`, so its events say which hosts were
reached or refused.  Envoy and seekrit also see inside the hosts they read,
so theirs say which requests were made and what they got (Envoy also says
how much each tunnel carried); seekrit's say which secrets went in (by name:
it never logs a value).

Everything in the log came from the dev container (a host name is whatever
it asked for), so every field is cleaned before it's shown, and an egress
rule is only suggested for a host that could be one.
"""
import datetime
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from . import gateway, jdata, review

# The actions an event can have.
TUNNEL = "tunnel"      # passed through untouched (Squid: every CONNECT it allows)
INTERCEPT = "intercept"  # the gateway reads it (seekrit)
DENY = "deny"          # refused at the connection
REQUEST = "request"    # one request the gateway read, and what it got
CLOSED = "closed"      # a tunnel ended, with what it carried

MAX_FIELD_CHARS = 512


@dataclass(frozen=True)
class Event:
    """One thing the gateway did, whichever gateway."""

    time: datetime.datetime
    action: str
    host: str
    port: int | None = None
    method: str | None = None
    path: str | None = None
    status: int | None = None
    reason: str | None = None
    injected: tuple[str, ...] = ()
    bytes_up: int | None = None
    bytes_down: int | None = None

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data: every field, the time in UTC."""
        return {
            "time": self.time.astimezone(datetime.UTC).isoformat(),
            "action": self.action, "host": self.host, "port": self.port,
            "method": self.method, "path": self.path, "status": self.status,
            "reason": self.reason, "injected": list(self.injected),
            "bytes-up": self.bytes_up, "bytes-down": self.bytes_down,
        }

    def line(self) -> str:
        """The event as one line, for a terminal: every field cleaned."""
        when = self.time.astimezone().strftime("%H:%M:%S")
        where = _where(self.host, self.port)
        if self.action == REQUEST:
            text = f"{_clean(self.method or '?')} {where}{_clean(self.path or '')} -> {self.status}"
            if self.injected:
                text += f"  with {', '.join(_clean(n) for n in self.injected)}"
        elif self.action == DENY:
            text = f"refused {where}" + (f" ({_clean(self.reason)})" if self.reason else "")
        elif self.action == CLOSED:
            text = f"closed {where} ({self.bytes_up or 0} bytes out, {self.bytes_down or 0} in)"
        else:
            text = f"{self.action} {where}"
        return f"{when}  {text}"


def _clean(value: str) -> str:
    return review.clean(value[:MAX_FIELD_CHARS])


def _where(host: str, port: int | None) -> str:
    return _clean(host) + (f":{port}" if port is not None else "")


# ---------------------------------------------------------------------
# The parsers: each takes one log line, and gives its event, if it's one.

# Squid's native access log line: TIME ELAPSED CLIENT CODE/STATUS BYTES METHOD URL USER HIER TYPE
_SQUID = re.compile(
    r"(?P<time>\d+\.\d+)\s+\d+\s+\S+\s+(?P<code>[A-Z_]+)/(?P<status>\d{3})\s+\d+\s+"
    r"(?P<method>[A-Z]+)\s+(?P<url>\S+)\s"
)
_AUTHORITY = re.compile(r"(?P<host>[^:/\s]+)(?::(?P<port>\d{1,5}))?")


def parse_squid(line: str) -> Event | None:
    """An event from a line of Squid's access log; None for any other line."""
    match = _SQUID.match(line.strip())
    if match is None:
        return None
    when = datetime.datetime.fromtimestamp(float(match["time"]), datetime.UTC)
    method, url, code = match["method"], match["url"], match["code"]
    if method == "CONNECT":
        authority = _AUTHORITY.fullmatch(url)
        if authority is None:
            return None
        host, port = authority["host"], int(authority["port"]) if authority["port"] else 443
        path = None
    else:
        # A plain request: Squid refuses them all (only CONNECT gets through).
        rest = url.split("://", 1)[-1]
        authority = _AUTHORITY.match(rest)
        if authority is None:
            return None
        host = authority["host"]
        port = int(authority["port"]) if authority["port"] else 80
        path = rest[authority.end():] or "/"
    if "DENIED" in code:
        return Event(when, DENY, host.lower(), port, method if path else None, path, int(match["status"]))
    if method == "CONNECT":
        return Event(when, TUNNEL, host.lower(), port, status=int(match["status"]))
    return Event(when, REQUEST, host.lower(), port, method, path, int(match["status"]))


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def parse_seekrit(line: str) -> Event | None:
    """An event from a line of seekrit-proxy's JSON log (`SEEKRIT_LOG_FORMAT=json`);
    None for any line that isn't one of its access events."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("target") != "seekrit_access":
        return None
    try:
        when = datetime.datetime.fromisoformat(str(data.get("timestamp")))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    host = _str(data.get("host"))
    if not host:
        return None
    port = _int(data.get("port"))
    kind = data.get("event")
    if kind == "connect":
        action = _str(data.get("action"))
        if action not in (TUNNEL, INTERCEPT, DENY):
            return None
        return Event(when, action, host, port, reason=_str(data.get("reason")))
    if kind == "request":
        injected = _str(data.get("injected")) or ""
        return Event(
            when, REQUEST, host, port,
            method=_str(data.get("method")), path=_str(data.get("path")),
            status=_int(data.get("status")),
            injected=tuple(name for name in injected.split(",") if name),
        )
    if kind == "tunnel_closed":
        return Event(
            when, CLOSED, host, port, reason=_str(data.get("error")),
            bytes_up=_int(data.get("bytes_up")), bytes_down=_int(data.get("bytes_down")),
        )
    return None


# Envoy's access log types (`%ACCESS_LOG_TYPE%`): a tunnel opened; a request
# (or tunnel) ended.
_ENVOY_TUNNEL = "DownstreamTunnelSuccessfullyEstablished"
_ENVOY_END = "DownstreamEnd"
# The Envoy gateway's route names (gateway_envoy.py): a `CONNECT` to a host it
# reads, and its refusal of a read host's request.
_ENVOY_INTERCEPT = "intercept "
_ENVOY_REFUSED = "refused"


def parse_envoy(line: str) -> Event | None:
    """An event from a line of the Envoy gateway's JSON access log (see
    `ACCESS_LOG_FORMAT` in `gateway_envoy.py`); None for any other line.

    A `CONNECT` (`plane: connect`) logs once when its tunnel opens, passed
    through or to a host the gateway reads (its route named `intercept
    HOST:PORT`), and once when it ends.  One no rule allows ends with a 404
    and the `NR` (no route) flag; any other failure (such as `DF`, a name
    that didn't resolve) ends a tunnel the rules allowed.  Each request to a
    read host (`plane: request`) logs once, with the rule's host and port,
    which come from the gateway's configuration, not the request.
    """
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    kind, method = _str(data.get("type")), _str(data.get("method"))
    if kind not in (_ENVOY_TUNNEL, _ENVOY_END) or not method:
        return None
    try:
        when = datetime.datetime.fromisoformat(str(data.get("time")))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    status = _int(data.get("code"))
    if data.get("plane") == "request":
        return _envoy_request(data, when, method, status)
    authority = _AUTHORITY.fullmatch(_str(data.get("authority")) or "")
    if authority is None:
        return None
    host = authority["host"].lower()
    port = int(authority["port"]) if authority["port"] else (443 if method == "CONNECT" else 80)
    flags = _str(data.get("flags")) or ""
    if kind == _ENVOY_TUNNEL:
        read = (_str(data.get("route")) or "").startswith(_ENVOY_INTERCEPT)
        return Event(when, INTERCEPT if read else TUNNEL, host, port, status=status)
    if "NR" in flags.split(","):
        if method == "CONNECT":
            return Event(when, DENY, host, port, status=status, reason="no_rule")
        return Event(when, DENY, host, port, method, _str(data.get("path")) or "/", status, "no_rule")
    return Event(
        when, CLOSED, host, port, status=status,
        reason=None if status == 200 else (flags if flags not in ("", "-") else f"status {status}"),
        bytes_up=_int(data.get("bytes_up")), bytes_down=_int(data.get("bytes_down")),
    )


def _envoy_request(data: dict[str, object], when: datetime.datetime, method: str, status: int | None) -> Event | None:
    """A request to a host the Envoy gateway reads: refused by it (its
    `refused` route, a 403), or sent on, with what came back."""
    host = _str(data.get("host"))
    port = _str(data.get("port"))
    if not host or not port or not port.isdigit():
        return None
    refused = data.get("route") == _ENVOY_REFUSED
    flags = _str(data.get("flags")) or ""
    # The secrets filter's, on a host whose rules name secrets: why it refused
    # the request, or the names it filled in.  Envoy writes null (or `-`) for none.
    filter_refused = _str(data.get("refused"))
    injected = _str(data.get("injected")) or ""
    if refused:
        reason: str | None = "no_rule"
    elif filter_refused and filter_refused != "-":
        reason = filter_refused
    else:
        reason = flags if flags not in ("", "-") else None
    return Event(
        when, REQUEST, host, int(port), method, _str(data.get("path")) or "/", status, reason,
        bytes_up=_int(data.get("bytes_up")), bytes_down=_int(data.get("bytes_down")),
        injected=tuple(name for name in injected.split(",") if name and name != "-"),
    )


# A gateway's parser: one log line, and its event, if it's one.
Parser = Callable[[str], "Event | None"]

PARSERS: dict[str, Parser] = {
    "squid": parse_squid,
    "seekrit": parse_seekrit,
    "envoy": parse_envoy,
}


def parse(lines: Iterable[str], parser: Parser) -> list[Event]:
    """The events in `lines`, in order."""
    return [event for event in map(parser, lines) if event is not None]


# ---------------------------------------------------------------------
# The summary


@dataclass
class Summary:
    """The events, counted."""

    hosts: Counter[tuple[str, int | None, str]] = field(default_factory=Counter)
    reasons: dict[tuple[str, int | None], set[str]] = field(default_factory=dict)
    requests: Counter[tuple[str, str, str, int | None, tuple[str, ...]]] = field(default_factory=Counter)
    injected: Counter[str] = field(default_factory=Counter)
    # Refused plain HTTP requests, not CONNECTs: a rule for them is `http`.
    plain: set[tuple[str, int | None]] = field(default_factory=set)

    def add(self, event: Event) -> None:
        """Count one event."""
        if event.action in (TUNNEL, INTERCEPT, DENY):
            self.hosts[(event.host, event.port, event.action)] += 1
            if event.action == DENY and event.reason:
                self.reasons.setdefault((event.host, event.port), set()).add(event.reason)
            if event.action == DENY and event.method:
                self.plain.add((event.host, event.port))
        if event.action == REQUEST:
            self.requests[(event.host, event.method or "?", event.path or "", event.status, event.injected)] += 1
            self.injected.update(event.injected)

    def lines(self) -> list[str]:
        """The summary, for a terminal: every field from the log cleaned."""
        out: list[str] = []
        if not self.hosts and not self.requests:
            return ["Nothing in the gateway's log yet."]
        if self.hosts:
            out.append("Hosts:")
            for (host, port, action), count in sorted(self.hosts.items(), key=lambda i: (i[0][0], i[0][1] or 0, i[0][2])):
                reasons = sorted(self.reasons.get((host, port), ())) if action == DENY else []
                why = f" ({', '.join(_clean(r) for r in reasons)})" if reasons else ""
                out.append(f"  {_where(host, port):<40} {action:<9} {count:>5}{why}")
        if self.requests:
            out.append("Requests the gateway read:")
            for (host, method, path, status, injected), count in sorted(
                self.requests.items(), key=lambda i: (i[0][0], i[0][2], i[0][1], i[0][3] or 0),
            ):
                names = f"  with {', '.join(_clean(n) for n in injected)}" if injected else ""
                out.append(f"  {_clean(method)} {_clean(host)}{_clean(path)} -> {status}  x{count}{names}")
        if self.injected:
            out.append("Secrets put into requests:")
            out.extend(f"  {_clean(name):<30} {count:>5}" for name, count in sorted(self.injected.items()))
        suggestions = self.suggestions()
        if suggestions:
            out.append(
                "Refused hosts.  If the container needs one, its rule goes in the project's "
                "`egress` (and is reviewed at the next pull); for example:"
            )
            out.extend(f"  - {s}" for s in suggestions)
        return out

    def suggestions(self) -> list[str]:
        """An egress rule for each refused host that could be one, never one it reached.

        Only a suggestion: a `tls` rule (or `http`, for a plain request),
        the least the gateway would need.
        """
        reached = {(host, port) for (host, port, action) in self.hosts if action != DENY}
        out: list[str] = []
        for host, port, action in sorted(self.hosts, key=lambda k: (k[0], k[1] or 0)):
            if action != DENY or (host, port) in reached or "*" in host or not gateway.host_is_valid(host):
                continue
            protocol, default = ("http", 80) if (host, port) in self.plain else ("tls", 443)
            rule = f"{{host: {host}, protocol: {protocol}" + ("" if port in (None, default) else f", port: {port}") + "}"
            if rule not in out:
                out.append(rule)
        return out


def summarize(events: Iterable[Event]) -> Summary:
    """The events, counted."""
    summary = Summary()
    for event in events:
        summary.add(event)
    return summary
