#SPDX:MIT
"""Turning the egress rules into a network gateway's configuration.

The rules come from the universal configuration file: the accepted ones
(`egress.rules`), or, until a review can make an acceptance (step 15 of
docs/contributors/projects-v1.md), the project file's own (`current-project-setup`).

Each gateway enforces what it can.  A rule it can't enforce is refused, with
the reason; it's never dropped, or loosened into something broader.  See
step 9 of docs/contributors/projects-v1.md.
"""
import re
from dataclasses import dataclass, field

from . import jdata
from .err import ConfigErr
from .user_config import UserConfig

DEFAULT_PORTS = {"tls": 443, "https": 443, "http": 80}

_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")


@dataclass(frozen=True)
class Rule:
    """One egress rule, with its port worked out."""

    host: str
    protocol: str
    port: int
    methods: tuple[str, ...] = ()

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "host": self.host,
            "protocol": self.protocol,
            "port": self.port,
            "methods": list(self.methods),
        }


@dataclass
class Report:
    """What the gateway enforces, and what it refuses, and why."""

    gateway: str
    # Where the rules came from: `accepted`, or `project` (unreviewed).
    source: str
    enforced: list[Rule] = field(default_factory=list)
    refused: list[tuple[Rule, str]] = field(default_factory=list)

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data, for the caller to show the user."""
        return {
            "gateway": self.gateway,
            "source": self.source,
            "enforced": [rule.to_json() for rule in self.enforced],
            "refused": [
                {"rule": rule.to_json(), "reason": reason} for rule, reason in self.refused
            ],
        }


class RuleErr(ValueError):
    """An egress rule isn't what the generator would have let through."""


def rules_from(config: UserConfig) -> tuple[list[Rule], str]:
    """The egress rules in the universal configuration file, and where they came from."""
    try:
        entries, source = config.egress_rules()
    except ConfigErr as err:
        raise RuleErr(str(err)) from err
    return [_rule(entry, i) for i, entry in enumerate(entries)], source


def _rule(entry: jdata.DictJsonData, i: int) -> Rule:
    at = f"egress[{i}]"
    try:
        host = jdata.req_str(entry, "host")
        protocol = jdata.req_str(entry, "protocol")
        port = jdata.find_int(entry, "port")
        methods = jdata.find_str_list(entry, "methods")
    except (KeyError, TypeError, ValueError) as err:
        raise RuleErr(f"{at}: {err}") from err
    if not host_is_valid(host):
        raise RuleErr(f"{at}.host: {host!r} isn't a host name")
    if protocol not in DEFAULT_PORTS:
        raise RuleErr(f"{at}.protocol: {protocol!r} isn't known")
    if port is None:
        port = DEFAULT_PORTS[protocol]
    if not 0 < port < 65536:
        raise RuleErr(f"{at}.port: {port!r} isn't a port")
    if not all(m.isascii() and m.isalpha() and m.isupper() for m in methods):
        raise RuleErr(f"{at}.methods: expected upper-case method names")
    return Rule(host, protocol, port, tuple(methods))


def host_is_valid(host: str) -> bool:
    """A lower-case ASCII host name of two or more labels, or `*.` and one.

    The generator has checked this already; checking again keeps anything
    else out of the gateway's configuration, which is a list of patterns.
    """
    name = host.removeprefix("*.")
    labels = name.split(".")
    return (
        len(labels) >= 2
        and len(name) <= 253
        and all(len(label) <= 63 and _LABEL.fullmatch(label) for label in labels)
    )


# ---------------------------------------------------------------------
# Squid


SQUID = "squid"


def squid_refusal(rule: Rule) -> str | None:
    """Why the Squid gateway can't enforce `rule`, if it can't.

    It sees only `CONNECT host:port` for TLS (it doesn't look inside), and its
    firewall lets only TCP 443 out (N2, N4).  So it enforces `tls` rules on
    port 443, and nothing else yet.
    """
    if rule.protocol == "https":
        return (
            "`https` needs a gateway that looks inside TLS to check the request; "
            "the Squid gateway only sees the host name (use `tls`, without `methods`)"
        )
    if rule.protocol == "http":
        return (
            "plain `http` needs port 80 through the gateway's firewall and Squid's HTTP "
            "handling, which the Squid gateway doesn't have yet (use `tls` on 443 if the host has it)"
        )
    if rule.port != 443:
        return f"the gateway's firewall only lets TCP port 443 out, not {rule.port}"
    if rule.methods:
        return "the gateway can't see the methods inside `tls`"
    return None


def squid_pattern(host: str) -> str:
    """The `dstdom_regex` line for a host rule.

    `host` matches only that name; `*.example.com` matches its subdomains,
    at any depth, but not `example.com` itself.
    """
    if host.startswith("*."):
        return "^.+\\." + re.escape(host[2:]) + "$"
    return "^" + re.escape(host) + "$"


def squid(rules: list[Rule], source: str) -> tuple[Report, str]:
    """The Squid gateway's report, and its host list (for `dstdom_regex -i`)."""
    report = Report(SQUID, source)
    for rule in rules:
        reason = squid_refusal(rule)
        if reason is None:
            report.enforced.append(rule)
        else:
            report.refused.append((rule, reason))
    lines = [
        "# The hosts the dev container may reach, on TCP 443: one pattern per line,",
        "# for Squid's `dstdom_regex`.  Made by Surf Scoter from the egress rules"
        + (" (unreviewed, from the project file)." if source == "project" else "."),
    ]
    lines.extend(sorted({squid_pattern(rule.host) for rule in report.enforced}))
    return report, "\n".join(lines) + "\n"
