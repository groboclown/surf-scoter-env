#!/bin/sh
set -eu

# This service owns the network namespace and installs the outbound firewall.
# Squid joins this namespace without NET_ADMIN.

# Find the isolated network's interface: the container's only interface other
# than loopback and the uplink, which carries the default route.  Docker assigns
# its subnet per environment, so the script finds it here rather than writing
# it into the rules.  Anything unexpected stops the container, so the gateway
# never starts with the wrong rules.
uplink_if=$( ip route show default | awk '{ for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit } }' )
if [ -z "${uplink_if}" ] ; then
  >&2 echo "No default route; can't tell the uplink network from the isolated one."
  exit 1
fi
isolated_if=""
for path in /sys/class/net/* ; do
  name="${path##*/}"
  if [ "${name}" = "lo" ] || [ "${name}" = "${uplink_if}" ] ; then
    continue
  fi
  if [ -n "${isolated_if}" ] ; then
    >&2 echo "Expected one isolated network interface, found more (${isolated_if}, ${name})."
    exit 1
  fi
  isolated_if="${name}"
done
if [ -z "${isolated_if}" ] ; then
  >&2 echo "No isolated network interface found."
  exit 1
fi
echo "Uplink interface: ${uplink_if}; isolated interface: ${isolated_if}"

# The DNS servers the container runtime configured: Docker's embedded resolver,
# or Podman's DNS on each network.  Only these may receive DNS queries.
dns_servers=$( awk '$1 == "nameserver" && $2 ~ /^[0-9.]+$/ { printf "%s%s", sep, $2; sep = ", " }' /etc/resolv.conf )
if [ -z "${dns_servers}" ] ; then
  >&2 echo "No IPv4 nameserver in /etc/resolv.conf."
  exit 1
fi
echo "DNS servers: ${dns_servers}"

# Create our own table without changing Docker's tables/chains.
nft delete table inet egress 2>/dev/null || true
nft -D "isolated_if=\"${isolated_if}\"" -D "dns_servers={ ${dns_servers} }" -f /usr/local/etc/egress.nft
nft list table inet egress

# Keep the container alive to keep the network forwarding active.  The shell
# stays PID 1 so the trap handles SIGTERM; PID 1 has no default signal
# handling, so an exec'd `sleep` would ignore it.
trap 'exit 0' TERM INT
sleep infinity &
wait $!
