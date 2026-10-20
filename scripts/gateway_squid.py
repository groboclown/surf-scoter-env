#SPDX:MIT
"""Write the Squid network gateway's configuration from the egress rules.

Usage: gateway_squid.py UNIVERSAL.json OUTPUT

The setup's `network-gateway: runs: write-config` runs this.  It reads the
egress rules from the universal configuration file (see
`surf_scoter_lib.gateway`), writes the gateway's host list to OUTPUT (which
the docker-compose file mounts into the gateway), and prints a JSON report
of what it enforces and what it refuses.

When it refuses any rule, it writes nothing and exits 1: a rule is never
dropped, or loosened.
"""
import json
import os
import sys
from collections.abc import Sequence

from surf_scoter_lib import gateway
from surf_scoter_lib.err import SurfScoterErr

MAX_UNIVERSAL_SIZE = 4 * 1024 * 1024


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    if len(argv) != 3:
        print(f"Usage: {argv[0] if argv else 'gateway_squid.py'} UNIVERSAL.json OUTPUT", file=sys.stderr)
        return 2
    try:
        with open(argv[1], "rb") as inp:
            data = inp.read(MAX_UNIVERSAL_SIZE + 1)
        if len(data) > MAX_UNIVERSAL_SIZE:
            raise SurfScoterErr(f"{argv[1]}: larger than {MAX_UNIVERSAL_SIZE} bytes")
        suc = json.loads(data)
        if not isinstance(suc, dict):
            raise SurfScoterErr(f"{argv[1]}: not a universal configuration file")
        rules, source = gateway.rules_from(suc)
    except (OSError, ValueError, SurfScoterErr) as err:
        print(f"{argv[0]}: {err}", file=sys.stderr)
        return 2
    report, text = gateway.squid(rules, source)
    print(json.dumps(report.to_json()))
    if report.refused:
        return 1
    # The gateway runs as its own user, which must be able to read it; it
    # holds nothing secret.  Replaced whole, so it's never half-written.
    temp = argv[2] + ".tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
    os.chmod(temp, 0o644)  # nosec: B103  # The gateway's user reads it; nothing secret.
    os.replace(temp, argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
