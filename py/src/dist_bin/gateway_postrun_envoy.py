#SPDX:MIT
"""Give the running Envoy gateway its secrets.

Usage: gateway_postrun_envoy.py UNIVERSAL.json CONTAINER

The setup's `dev-env: runs: network-gateway-post-start` runs this once the
gateway (CONTAINER) has started, and `scoter gateway-post-start` runs it
again after the gateway restarts.  `surf_scoter_lib.gateway_secrets` does
the work: it reads the values of the secrets the egress rules name from your
secret store, and sends them to the gateway's `envoy-inject`, which passes
them to its inject listener: a Unix socket only the gateway's own user can
open.  They're held only in the gateway's memory.

Unlike the seekrit gateway, Envoy can't resolve a seekrit.dev token: the
store must give the values themselves (`[secrets]`).  Its socket stays after
the delivery, but takes no second one: restart the gateway to give the
secrets again.  When the rules name no secrets, the gateway has no inject
socket, and this does nothing.
"""
import sys
from collections.abc import Sequence

import gateway_envoy
from surf_scoter_lib import gateway_secrets

# `envoy-inject`, in the gateway's image, with the same exit codes as
# `seekrit-proxy inject` (`gateway_secrets.INJECT_*`).
INJECTOR = gateway_secrets.Injector(
    command=("/usr/local/bin/envoy-inject", "--socket", gateway_envoy.INJECT_SOCKET),
    allow_token=False,
    socket_closes=False,
    skip_without_names=True,
)


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    return gateway_secrets.run(argv, INJECTOR)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
