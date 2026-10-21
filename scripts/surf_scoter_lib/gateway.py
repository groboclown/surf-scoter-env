#SPDX:MIT
"""Turning the egress rules into a network gateway's configuration.

The rules come from the universal configuration file: the accepted ones
(`egress.rules`), or, until a review can make an acceptance (step 15 of
docs/contributors/projects-v1.md), the project file's own (`current-project-setup`).

Each gateway enforces what it can.  A rule it can't enforce is refused, with
the reason; it's never dropped, or loosened into something broader.  Each
gateway's own translation is in its script (`gateway_squid.py` for Squid),
which the setup's `network-gateway: runs: write-config` runs.  See step 9 of
docs/contributors/projects-v1.md.
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
    enforced: list[Rule] = field(default_factory=list)
    refused: list[tuple[Rule, str]] = field(default_factory=list)

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data, for the caller to show the user."""
        return {
            "gateway": self.gateway,
            "enforced": [rule.to_json() for rule in self.enforced],
            "refused": [
                {"rule": rule.to_json(), "reason": reason} for rule, reason in self.refused
            ],
        }


class RuleErr(ValueError):
    """An egress rule isn't what the generator would have let through."""


def rules_from(config: UserConfig) -> list[Rule]:
    """The accepted egress rules in the universal configuration file; none without any."""
    try:
        entries = config.egress_rules()
    except ConfigErr as err:
        raise RuleErr(str(err)) from err
    return rules_of(entries)


def rules_of(entries: list[jdata.DictJsonData]) -> list[Rule]:
    """Egress rules from their entries (the accepted shape, or the project file's)."""
    return [_rule(entry, i) for i, entry in enumerate(entries)]


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
