# Squid Gateway Attack Vectors

The weak points of the Squid network gateway ([`containers/network-gateway-squid`](../../containers/network-gateway-squid/)), the default, measured against the [network's required properties](network.md#required-properties).  The weak points every gateway shares are on the [network attack vectors](network-attack-vectors.md) page; the other gateway's are on the [seekrit gateway](network-gateway-seekrit-attack-vectors.md) page.  See the [attack vector overview](attack-vectors.md) for how to read the severity and status labels.

Squid only sees each `CONNECT`: the host and port, never what's inside.  It enforces `tls` rules on port 443, and refuses every other kind of rule, so the project can't ask it for more than it can check.


## Attack surface

| Input | Controlled by | Handled in |
| --- | --- | --- |
| Egress rules | Your configuration (the project's suggestion, reviewed at `scoter pull`) | [`gateway_squid.py`](../../scripts/gateway_squid.py) turns them into the host list; [`squid.conf`](../../containers/network-gateway-squid/squid.conf) enforces it |
| `CONNECT` requests, hostnames | Code in the dev container | [`squid.conf`](../../containers/network-gateway-squid/squid.conf) |
| DNS answers for allowed domains | Whoever runs those domains' DNS | [`blocked-destinations.txt`](../../containers/network-gateway-squid/blocked-destinations.txt), [`egress.nft`](../../containers/firewall-router/egress.nft) |
| The run's CA | `scoter run` | [`gateway_start.py`](../../scripts/surf_scoter_lib/gateway_start.py), [`docker-compose.yaml`](../../defaults/docker-compose.yaml) |


## Summary

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [SQUID-1](#squid-1-allowed-names-resolving-to-forbidden-addresses) | Allowed names resolving to forbidden addresses | Medium | Mitigated |
| [SQUID-2](#squid-2-a-subverted-squid) | A subverted Squid | Medium | Mitigated |
| [SQUID-3](#squid-3-the-runs-ca-with-a-gateway-that-doesnt-use-it) | The run's CA, with a gateway that doesn't use it | Low | Mitigated |


## Vectors

### SQUID-1: Allowed names resolving to forbidden addresses

**Severity:** Medium.  **Status:** Mitigated (N3, N4).  Formerly NET-5.
**Where:** the rule order in [`squid.conf`](../../containers/network-gateway-squid/squid.conf); [`blocked-destinations.txt`](../../containers/network-gateway-squid/blocked-destinations.txt); the `blocked_v4` set in [`egress.nft`](../../containers/firewall-router/egress.nft).

Squid checks the hostname, then resolves it itself.  An allowed domain whose DNS an attacker controls can point anywhere.  Squid's blocked destination list and the firewall's blocked set keep it off private and special-purpose addresses.  The two lists are maintained separately and have already drifted: the gateway blocks `192.88.99.0/24`, and the firewall doesn't.

**Red team:** point an allowed name at private, loopback, link-local and metadata addresses, and at IPv6 addresses (`AAAA` records).  Try DNS rebinding between Squid's check and its connection.  Look for public-looking ranges that reach the host or LAN in your setup, such as a runtime that uses public address pools.

**When changing this code:** keep the domain check before the address check in `squid.conf`, so unlisted names are never resolved.  Keep the two blocked lists in sync.

### SQUID-2: A subverted Squid

**Severity:** Medium.  **Status:** Mitigated (N4).  Formerly NET-10.
**Where:** [`network-gateway-squid/Dockerfile`](../../containers/network-gateway-squid/Dockerfile), [`egress.nft`](../../containers/firewall-router/egress.nft).

Squid parses hostile input.  If it's subverted, the firewall still limits it to TCP 443 on public IPv4 addresses, but not to the allowed domains.  It never holds a secret, or the run's CA key ([SQUID-3](#squid-3-the-runs-ca-with-a-gateway-that-doesnt-use-it)), so it can see where data goes, but not read it.

**Red team:** check the Squid version for known issues, and try malformed `CONNECT` requests.  From inside the gateway's namespace, confirm what the firewall actually allows.

**When changing this code:** keep Squid unprivileged and without capabilities, and never give it `NET_ADMIN`.

### SQUID-3: The run's CA, with a gateway that doesn't use it

**Severity:** Low.  **Status:** Mitigated.
**Where:** `make_ca` and `gateway_ca_dir` in [`gateway_start.py`](../../scripts/surf_scoter_lib/gateway_start.py); the gateway's `/user-space/ca` and the dev container's `/etc/scoter/gateway-ca.pem` in [`docker-compose.yaml`](../../defaults/docker-compose.yaml); `dev-env: values: network-gateway-reads-tls` in [`setup.yaml`](../../defaults/setup.yaml).

`scoter run` makes a new CA for every run, whatever the gateway, and the dev container trusts it for every host.  Squid never uses it: with `network-gateway-reads-tls: 'no'`, its `/user-space/ca` is an empty directory, and the key stays on the host, readable only by you (mode 600, in a 700 directory), until the run ends, when it's removed (as `scoter kill` and `scoter cleanup` remove it).  What's left: while the run lasts, whoever can read your files can sign certificates the dev container trusts.  They could already change the container's image or settings.

**Red team:** check the key's mode and directory during a run, that Squid's `/user-space/ca` is empty, and that the CA is gone after the run ends, is killed, or is cleaned up after a crash.  Set `network-gateway-reads-tls: 'yes'` with Squid, and confirm the key is then readable inside the gateway: the setting, not the gateway, decides.

**When changing this code:** a gateway that doesn't read TLS must never be given the key.  Keep the CA new for each run.


## Sensitive code

Changes to these files can weaken the Squid gateway, and need a security review:

* [`containers/network-gateway-squid/`](../../containers/network-gateway-squid/): the proxy rules and the blocked destinations.
* [`scripts/gateway_squid.py`](../../scripts/gateway_squid.py): what Squid is told to allow, and what it refuses.
* [`scripts/surf_scoter_lib/gateway_start.py`](../../scripts/surf_scoter_lib/gateway_start.py): the run's CA, and which gateway gets it.
