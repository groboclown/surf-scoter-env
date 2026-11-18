#SPDX:MIT
"""Give the running seekrit gateway its secrets.

Usage: gateway_postrun_seekrit.py UNIVERSAL.json CONTAINER

The setup's `dev-env: runs: network-gateway-post-start` runs this once the
gateway (CONTAINER) has started, and `scoter gateway-post-start` runs it
again after the gateway restarts.  `surf_scoter_lib.gateway_secrets` does
the work: it reads the secret names the egress rules give, and their values
from your secret store (or a seekrit.dev token that gives them), and sends
them to the gateway's `seekrit-proxy inject`, which passes them over a
socket only the gateway's own user can reach.

The gateway takes one delivery; after that its socket is gone.  So a socket
still missing well after the container started means it already has its
secrets, and this stops then rather than waiting on.  With no secrets named,
it's sent an empty set, which closes its socket.
"""
import sys
from collections.abc import Sequence

import gateway_seekrit
from surf_scoter_lib import gateway_secrets

INJECTOR = gateway_secrets.Injector(
    command=("/seekrit-proxy", "inject", "--socket", gateway_seekrit.INJECT_SOCKET),
    allow_token=True,
    socket_closes=True,
    skip_without_names=False,
)


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    return gateway_secrets.run(argv, INJECTOR)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
