#SPDX:MIT
"""Write the Squid network gateway's configuration from the egress rules.

Usage: gateway_squid.py UNIVERSAL.json OUTPUT

The setup's `network-gateway: runs: write-config` runs this.  It reads the
egress rules from the universal configuration file (see
`surf_scoter_lib.gateway`), works out what Squid can enforce of them, writes the gateway's host list to OUTPUT (which
the docker-compose file mounts into the gateway), and prints a JSON report
of what it enforces and what it refuses.

When it refuses any rule, it writes nothing and exits 1: a rule is never
dropped, or loosened.
"""
import json
import os
import re
import sys
from collections.abc import Sequence

from surf_scoter_lib import UserConfig, fileutil, gateway
from surf_scoter_lib.err import SurfScoterErr

MAX_UNIVERSAL_BYTES = 4 * 1024 * 1024
# The only port the gateway's firewall lets out.
FIREWALL_PORT = 443
# The host list's mode: the gateway runs as its own user, which must read it.
MODE_HOST_LIST = 0o644
EXIT_REFUSED = 1
EXIT_ERROR = 2

# ---------------------------------------------------------------------
# The translation


SQUID = "squid"


def refusal(rule: gateway.Rule) -> str | None:
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
    if rule.port != FIREWALL_PORT:
        return f"the gateway's firewall only lets TCP port {FIREWALL_PORT} out, not {rule.port}"
    if rule.methods:
        return "the gateway can't see the methods inside `tls`"
    return None


def pattern(host: str) -> str:
    """The `dstdom_regex` line for a host rule.

    `host` matches only that name; `*.example.com` matches its subdomains,
    at any depth, but not `example.com` itself.
    """
    if host.startswith("*."):
        return "^.+\\." + re.escape(host[2:]) + "$"
    return "^" + re.escape(host) + "$"


def translate(rules: list[gateway.Rule]) -> tuple[gateway.Report, str]:
    """The Squid gateway's report, and its host list (for `dstdom_regex -i`)."""
    report = gateway.Report(SQUID)
    for rule in rules:
        reason = refusal(rule)
        if reason is None:
            report.enforced.append(rule)
        else:
            report.refused.append((rule, reason))
    lines = [
        "# The hosts the dev container may reach, on TCP 443: one pattern per line,",
        "# for Squid's `dstdom_regex`.  Made by Surf Scoter from your accepted egress rules.",
    ]
    lines.extend(sorted({pattern(rule.host) for rule in report.enforced}))
    return report, "\n".join(lines) + "\n"


# ---------------------------------------------------------------------
# The command


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    if len(argv) != 3:
        print(f"Usage: {argv[0] if argv else 'gateway_squid.py'} UNIVERSAL.json OUTPUT", file=sys.stderr)
        return EXIT_ERROR
    try:
        with open(argv[1], "rb") as inp:
            data = inp.read(MAX_UNIVERSAL_BYTES + 1)
        if len(data) > MAX_UNIVERSAL_BYTES:
            raise SurfScoterErr(f"{argv[1]}: larger than {MAX_UNIVERSAL_BYTES} bytes")
        suc = json.loads(data)
        if not isinstance(suc, dict):
            raise SurfScoterErr(f"{argv[1]}: not a universal configuration file")
        rules = gateway.rules_from(UserConfig(suc))
    except (OSError, ValueError, SurfScoterErr) as err:
        print(f"{argv[0]}: {err}", file=sys.stderr)
        return EXIT_ERROR
    report, text = translate(rules)
    print(json.dumps(report.to_json()))
    if report.refused:
        return EXIT_REFUSED
    # The gateway runs as its own user, which must be able to read it; it
    # holds nothing secret.  Replaced whole, so it's never half-written.
    temp = argv[2] + ".tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | fileutil.O_NOFOLLOW, MODE_HOST_LIST)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
    os.chmod(temp, MODE_HOST_LIST)
    os.replace(temp, argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
