#SPDX:MIT
"""Turning the egress rules into a network gateway's configuration.

The rules come from the universal configuration file: the configuration
in use's (`egress.rules`), or, for the checks on a copy being reviewed, its
project file's own (`current-project-setup`).

Each gateway enforces what it can.  A rule it can't enforce is refused, with
the reason; it's never dropped, or loosened into something broader.  Each
gateway's own translation is in its script (`gateway_squid.py` for Squid),
which the setup's `network-gateway: runs: write-config` runs.
"""
import re
from dataclasses import dataclass, field

from . import jdata
from .err import ConfigErr
from .user_config import UserConfig

DEFAULT_PORTS = {"tls": 443, "https": 443, "http": 80}
MAX_PORT = 65535
# DNS limits (RFC 1035): a whole name, and each label in it.
MAX_HOST_NAME_CHARS = 253
MAX_LABEL_CHARS = 63

_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
# A secret's name: what `{{seekrit:NAME}}` names (the generator's check).
_SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


@dataclass(frozen=True)
class Rule:
    """One egress rule, with its port worked out."""

    host: str
    protocol: str
    port: int
    methods: tuple[str, ...] = ()
    # For `https`: the secrets the gateway may put into requests to the host.
    secrets: tuple[str, ...] = ()

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "host": self.host,
            "protocol": self.protocol,
            "port": self.port,
            "methods": list(self.methods),
            "secrets": list(self.secrets),
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


# The setup's lists of the ports a gateway may use, in `dev-env: value-lists`.
PORTS_SECTION = "dev-env"
GATEWAY_PORTS = "network-gateway-ports"
GATEWAY_TLS_PORTS = "network-gateway-tls-ports"


def setup_ports(config: UserConfig, key: str) -> frozenset[int]:
    """The ports the setup's `dev-env: value-lists: KEY` names; none when it isn't set.

    Each entry must be a port number, written as text (`'443'`).  None means
    no port: every rule that needs one is refused, never let through.
    """
    where = f"the setup's `{PORTS_SECTION}: value-lists: {key}`"
    try:
        entries = config.value_list(PORTS_SECTION, key)
    except ConfigErr as err:
        raise RuleErr(f"{where}: {err}") from err
    ports: set[int] = set()
    for entry in entries:
        if not (entry.isascii() and entry.isdigit() and 0 < int(entry) <= MAX_PORT):
            raise RuleErr(f"{where} has {entry!r}, which isn't a TCP port")
        ports.add(int(entry))
    return frozenset(ports)


def ports_text(ports: frozenset[int]) -> str:
    """`443` or `443, 8443`, for messages; `none` for no ports."""
    return ", ".join(str(port) for port in sorted(ports)) or "none"


def rules_from(config: UserConfig) -> list[Rule]:
    """The configuration's egress rules in the universal configuration file; none without any."""
    try:
        entries = config.egress_rules()
    except ConfigErr as err:
        raise RuleErr(str(err)) from err
    return rules_of(entries)


def rules_of(entries: list[jdata.DictJsonData]) -> list[Rule]:
    """Egress rules from their entries (the universal file's `egress` shape, or the project file's)."""
    return [_rule(entry, i) for i, entry in enumerate(entries)]


def _rule(entry: jdata.DictJsonData, i: int) -> Rule:
    at = f"egress[{i}]"
    try:
        host = jdata.req_str(entry, "host")
        protocol = jdata.req_str(entry, "protocol")
        port = jdata.find_int(entry, "port")
        methods = jdata.find_str_list(entry, "methods")
        secrets = jdata.find_str_list(entry, "secrets")
    except (KeyError, TypeError, ValueError) as err:
        raise RuleErr(f"{at}: {err}") from err
    if not host_is_valid(host):
        raise RuleErr(f"{at}.host: {host!r} isn't a host name")
    if protocol not in DEFAULT_PORTS:
        raise RuleErr(f"{at}.protocol: {protocol!r} isn't known")
    if port is None:
        port = DEFAULT_PORTS[protocol]
    if not 0 < port <= MAX_PORT:
        raise RuleErr(f"{at}.port: {port!r} isn't a port")
    if not all(m.isascii() and m.isalpha() and m.isupper() for m in methods):
        raise RuleErr(f"{at}.methods: expected upper-case method names")
    if secrets:
        # The generator refuses these; a gateway gets nothing else either.
        if protocol != "https" or host.startswith("*."):
            raise RuleErr(f"{at}.secrets: only for an `https` rule on an exact host")
        if not all(_SECRET_NAME.fullmatch(name) for name in secrets) or len(set(secrets)) != len(secrets):
            raise RuleErr(f"{at}.secrets: expected distinct upper-case secret names")
    return Rule(host, protocol, port, tuple(methods), tuple(secrets))


def host_is_valid(host: str) -> bool:
    """A lower-case ASCII host name of two or more labels, or `*.` and one.

    The generator has checked this already; checking again keeps anything
    else out of the gateway's configuration, which is a list of patterns.
    """
    name = host.removeprefix("*.")
    labels = name.split(".")
    return (
        len(labels) >= 2
        and len(name) <= MAX_HOST_NAME_CHARS
        and all(len(label) <= MAX_LABEL_CHARS and _LABEL.fullmatch(label) for label in labels)
    )
