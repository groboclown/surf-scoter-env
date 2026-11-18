#SPDX:MIT
"""Write the seekrit network gateway's configuration from the egress rules.

Usage: gateway_seekrit.py UNIVERSAL.json OUTPUT

The setup's `network-gateway: runs: write-config` runs this.  It reads the
egress rules from the universal configuration file (see
`surf_scoter_lib.gateway`), works out what seekrit-proxy can enforce of
them, writes the proxy's configuration (TOML) to OUTPUT, which the
docker-compose file mounts into the gateway, and prints a JSON report of
what it enforces and what it refuses.

When it refuses any rule, it writes nothing and exits 1: a rule is never
dropped, or loosened.

It sees the secrets' names only, from the rules; never their values, nor
the user's secret store.  Those reach the gateway after it starts
(`network-gateway-post-start`).
"""
import json
import os
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field

from surf_scoter_lib import fileutil, gateway, universal
from surf_scoter_lib.err import SurfScoterErr

# The configuration's mode: the gateway runs as its own user, which must
# read it.  It holds no secret, only names.
MODE_CONFIG = 0o644
EXIT_REFUSED = 1
EXIT_ERROR = 2

SEEKRIT = "seekrit"

# Inside the gateway container.  The docker-compose file mounts the
# interception CA here, and a private tmpfs at the socket's directory.
LISTEN = "0.0.0.0:3128"
CA_CERT = "/user-space/ca/gateway-ca.pem"
CA_KEY = "/user-space/ca/gateway-ca-key.pem"
INJECT_SOCKET = "/run/seekrit/inject.sock"

# ---------------------------------------------------------------------
# The translation


def refusal(rule: gateway.Rule, ports: frozenset[int], tls_ports: frozenset[int]) -> str | None:
    """Why the seekrit gateway can't enforce `rule` on its own, if it can't.

    The firewall lets out only `ports` (the setup's `network-gateway-ports`),
    and the gateway reads TLS only on `tls_ports` (its
    `network-gateway-tls-ports`).  It matches host names exactly.
    """
    if rule.host.startswith("*."):
        return (
            "the seekrit gateway doesn't allow wildcard hosts, which it can't match; "
            "list each host the container needs"
        )
    if rule.port not in ports:
        return (
            f"the gateway's firewall only lets TCP {gateway.ports_text(ports)} out, not {rule.port} "
            f"(the setup's `dev-env: value-lists: {gateway.GATEWAY_PORTS}`)"
        )
    if rule.protocol == "https" and rule.port not in tls_ports:
        return (
            f"the gateway only looks inside TLS on TCP {gateway.ports_text(tls_ports)}, not {rule.port} "
            f"(the setup's `dev-env: value-lists: {gateway.GATEWAY_TLS_PORTS}`)"
        )
    return None


def _host_refusal(rules: list[gateway.Rule]) -> str | None:
    """Why one host's rules, together, can't be enforced, if they can't.

    The gateway either reads a host's traffic or passes it through untouched,
    and it decides which, and whether the port is allowed, when the
    connection is made, before any request is known.
    """
    tunnels = [r for r in rules if r.protocol == "tls"]
    if tunnels and len(tunnels) != len(rules):
        return (
            "the seekrit gateway either reads a host's traffic or passes it through untouched, "
            "so one host can't have both `tls` rules and `https` or `http` rules"
        )
    if len({r.protocol for r in rules}) > 1:
        # One rule set serves both, so an `https` rule's methods would apply
        # to plain HTTP too.
        return "the seekrit gateway can't tell a host's `https` rules from its `http` ones"
    if not tunnels and len({r.port for r in rules}) > 1:
        return (
            "the seekrit gateway checks a host's port before it reads a request, so a host's "
            "`https` and `http` rules must all be on one port"
        )
    return None


@dataclass
class HostRule:
    """One `[[forward.host]]` table."""

    host: str
    intercept: bool
    ports: list[int]
    methods: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)


def host_rules(rules: list[gateway.Rule]) -> list[HostRule]:
    """The proxy's host rules for rules it can enforce (see `refusal`).

    A host's `tls` rules become one tunnel rule, on all their ports.  Its
    `https` and `http` rules become intercepting rules; those with the same
    methods become one, with all their secrets, since the proxy uses the
    first rule that matches a request.  Rules naming methods come before
    those allowing any, so a narrower rule (and its secrets) is found first.
    A request two rules match gets the secrets of the first only: the other's
    are refused, never loosened.
    """
    by_host: dict[str, list[gateway.Rule]] = {}
    for rule in rules:
        by_host.setdefault(rule.host, []).append(rule)
    out: list[HostRule] = []
    for host, group in by_host.items():
        if group[0].protocol == "tls":
            out.append(HostRule(host, False, sorted({r.port for r in group})))
            continue
        merged: dict[frozenset[str], HostRule] = {}
        for rule in group:
            key = frozenset(rule.methods)
            entry = merged.setdefault(key, HostRule(host, True, [rule.port], sorted(key)))
            # Only an `https` rule carries secrets (the generator's check).
            entry.allow.extend(name for name in rule.secrets if name not in entry.allow)
        out.extend(sorted(merged.values(), key=lambda entry: not entry.methods))
    return out


def translate(
    rules: list[gateway.Rule], ports: frozenset[int], tls_ports: frozenset[int],
) -> tuple[gateway.Report, str]:
    """The seekrit gateway's report, and its configuration (TOML)."""
    report = gateway.Report(SEEKRIT)
    alone: list[gateway.Rule] = []
    for rule in rules:
        reason = refusal(rule, ports, tls_ports)
        if reason is None:
            alone.append(rule)
        else:
            report.refused.append((rule, reason))
    by_host: dict[str, list[gateway.Rule]] = {}
    for rule in alone:
        by_host.setdefault(rule.host, []).append(rule)
    for group in by_host.values():
        reason = _host_refusal(group)
        if reason is None:
            report.enforced.extend(group)
        else:
            report.refused.extend((rule, reason) for rule in group)
    return report, config_text(host_rules(report.enforced))


def _str(value: str) -> str:
    # A JSON string, in ASCII, is a TOML basic string.
    return json.dumps(value, ensure_ascii=True)


def _strs(values: Sequence[str]) -> str:
    return "[" + ", ".join(_str(v) for v in values) + "]"


def config_text(hosts: list[HostRule]) -> str:
    """The proxy's configuration: deny every host but these, read TLS only for
    the intercepting ones, and put secrets only into TLS."""
    lines = [
        "# seekrit-proxy's configuration for the Surf Scoter network gateway.",
        "# Made by Surf Scoter from your configuration's egress rules; it names",
        "# secrets, never their values.",
        "",
        "[forward]",
        f"listen = {_str(LISTEN)}",
        # A host without a rule is refused, never passed through.
        'unmatched_host_policy = "deny"',
        # A secret only ever leaves inside TLS.
        'plaintext_injection = "refuse"',
        f"ca_cert = {_str(CA_CERT)}",
        f"ca_key = {_str(CA_KEY)}",
    ]
    for host in hosts:
        lines += ["", "[[forward.host]]", f"match = {_str(host.host)}"]
        if not host.intercept:
            lines.append("intercept = false")
        lines.append("ports = [" + ", ".join(str(p) for p in host.ports) + "]")
        if host.methods:
            lines.append(f"methods = {_strs(host.methods)}")
        if host.allow:
            lines.append(f"allow = {_strs(host.allow)}")
    lines += [
        "",
        # The secrets come after the gateway starts, over a socket only its
        # own user can reach, and are held only in memory.
        "[secrets]",
        'source = "inject"',
        f"socket = {_str(INJECT_SOCKET)}",
        "",
        # A value an upstream echoes back is scrubbed from its response.
        "[redaction]",
        "enabled = true",
        "",
        # Never private, loopback or link-local addresses, whatever a name resolves to.
        "[egress]",
        "block_private_ips = true",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------
# The command


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    if len(argv) != 3:
        print(f"Usage: {argv[0] if argv else 'gateway_seekrit.py'} UNIVERSAL.json OUTPUT", file=sys.stderr)
        return EXIT_ERROR
    try:
        config = universal.read_universal(argv[1])
        rules = gateway.rules_from(config)
        ports = gateway.setup_ports(config, gateway.GATEWAY_PORTS)
        tls_ports = gateway.setup_ports(config, gateway.GATEWAY_TLS_PORTS)
    except (OSError, ValueError, SurfScoterErr) as err:
        print(f"{argv[0]}: {err}", file=sys.stderr)
        return EXIT_ERROR
    report, text = translate(rules, ports, tls_ports)
    print(json.dumps(report.to_json()))
    if report.refused:
        return EXIT_REFUSED
    # What's written must be what was meant: it parses, as TOML, to the rules.
    tomllib.loads(text)
    # Replaced whole, so it's never half-written.
    temp = argv[2] + ".tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | fileutil.O_NOFOLLOW, MODE_CONFIG)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
    os.chmod(temp, MODE_CONFIG)
    os.replace(temp, argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
