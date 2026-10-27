# Network Attack Vectors

The weak points of the [runtime network](network.md), measured against its [required properties](network.md#required-properties) (N1 to N8).  Contributors should treat changes to the code listed here as security-sensitive, and the red team can use each vector as a starting point.  See the [attack vector overview](attack-vectors.md) for how to read the severity and status labels.


## Attacker goals

Code in the dev container wants to:

* **exfiltrate** source code, credentials or anything else it can read;
* **move laterally** to the host's services, the LAN or VPN, or cloud metadata endpoints;
* **receive control** from outside, or offer a service that something on the host connects to.


## Attack surface

| Input | Controlled by | Handled in |
| --- | --- | --- |
| Egress rules | The project file's `egress`, as accepted | [`gateway_squid.py`](../../scripts/gateway_squid.py) turns them into the gateway's host list; [`squid.conf`](../../containers/network-gateway/squid.conf) enforces it |
| `CONNECT` requests, hostnames | Code in the dev container | [`squid.conf`](../../containers/network-gateway/squid.conf) |
| DNS answers for allowed domains | Whoever runs those domains' DNS | [`blocked-destinations.txt`](../../containers/network-gateway/blocked-destinations.txt), [`egress.nft`](../../containers/firewall-router/egress.nft) |
| DNS queries from the dev container | Code in the dev container | The container runtime's resolver |
| Network topology and start order | Your settings | [`docker-compose.yaml`](../../defaults/docker-compose.yaml), [firewall `entrypoint.sh`](../../containers/firewall-router/entrypoint.sh) |
| Forwarded ports | You, through the IDE | [`sshd_config`](../../containers/dev-env/sshd_config), IDE settings |


## Summary

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [NET-1](#net-1-allowed-domains-are-open-channels) | Allowed domains are open channels | High | By design |
| [NET-2](#net-2-the-project-controls-the-allowlist) | The project controls the allowlist | High | Mitigated |
| [NET-3](#net-3-dns-from-the-dev-container) | DNS from the dev container | High | Unverified |
| [NET-4](#net-4-proxy-bypass) | Proxy bypass | High | Mitigated |
| [NET-5](#net-5-allowed-names-resolving-to-forbidden-addresses) | Allowed names resolving to forbidden addresses | Medium | Mitigated |
| [NET-6](#net-6-environments-sharing-a-network) | Environments sharing a network | Medium | Unverified |
| [NET-7](#net-7-traffic-without-firewall-rules) | Traffic without firewall rules | Medium | Unverified |
| [NET-8](#net-8-reaching-the-host-through-the-runtime) | Reaching the host through the runtime | Medium | Unverified |
| [NET-9](#net-9-forwarded-ports) | Forwarded ports | Medium | By design |
| [NET-10](#net-10-a-subverted-gateway) | A subverted gateway | Medium | Mitigated |

Image builds also have open network access; see [DEV-3](dev-container-attack-vectors.md#dev-3-image-builds-run-outside-the-isolation).


## Vectors

### NET-1: Allowed domains are open channels

**Severity:** High.  **Status:** By design.
**Where:** [`squid.conf`](../../containers/network-gateway/squid.conf), the project's allowed domain list.

Once a domain is on the list, anything in the container can send it anything.  There's no TLS interception, so the gateway sees the hostname but not the content.  Allowing `.github.com` lets code push source to any repository it has credentials for, or post it as a gist.  Wildcards widen this: an entry starting with `.` covers every subdomain, including hosts that serve user content.

**Red team:** exfiltrate through each domain in a typical allowlist (repository pushes, gists, package uploads, issue comments).  Look for allowed wildcards that cover user-controlled hosts.

**When changing this code:** the default list should stay minimal.  Adding content inspection changes the trust model (a private certificate authority in the container, plaintext in Squid) and needs its own analysis.

### NET-2: The project controls the allowlist

**Severity:** High.  **Status:** Mitigated by [project verification](verification.md).
**Where:** the project file's `egress`; the egress checks in [`egress.rs`](../../host/src/config/egress.rs), [`public_suffix.rs`](../../host/src/config/public_suffix.rs) and [`checks.py`](../../scripts/surf_scoter_lib/checks.py); [`gateway_squid.py`](../../scripts/gateway_squid.py); `_write_gateway_config` in [`main_run.py`](../../scripts/main_run.py).

The project suggests the rules, but the gateway gets only the rules you accepted with `scoter verify`, from the verified store.  Until you accept them, nothing gets out.  The generator refuses hosts that aren't plain, lower-case DNS names (no IP addresses, a `*` only as a leading `*.`, at least two labels), and wildcards over a public suffix.  `gateway_squid.py` escapes each host into Squid's pattern, and stops the launch on a rule the gateway can't enforce.

What's left:

* **The checks are thin.**  The review flags plain `http` and hosts on the `verification: egress-hosts` lists; any other host is shown, but only as a rule.  [NET-1](#net-1-allowed-domains-are-open-channels) is the real risk of each one.
* **The public suffix list is off by default.**  `verification: path-lists: public-suffix-lists` is empty, so only the built-in `egress-refused-wildcards` list stops a wildcard such as `*.some-hosting-provider.example`.
* **Accepting without looking.**  `scoter verify --with-exemption` accepts new rules unseen ([VER-4](verification-attack-vectors.md#ver-4-exemptions-accept-what-nobody-reviewed)).
* **Checks added later don't apply** to projects already accepted ([VER-6](verification-attack-vectors.md#ver-6-setup-changes-that-dont-ask-for-a-new-review)).

**Red team:** ship a project whose rules contain very broad entries, such as `*.com`, a bare IP address, a wildcard over a hosting provider's user domains, or entries Squid might parse unexpectedly, and see what the review shows and the gateway allows.  Get a rule change accepted through `--with-exemption`.

**When changing this code:** validation belongs in the generator, before the rules reach the review, and again before they reach the gateway.  Keep the gateway's configuration made only from the accepted rules.

### NET-3: DNS from the dev container

**Severity:** High, if the channel exists.  **Status:** Unverified (N8).
**Where:** the `isolated` network in [`docker-compose.yaml`](../../defaults/docker-compose.yaml); the container runtime's resolver (Docker's embedded DNS, Podman's `aardvark-dns`).

The firewall limits the gateway's DNS, but the dev container has its own path to the runtime's resolver on the isolated network.  If that resolver forwards queries for external names, DNS becomes an exfiltration and command channel that bypasses the gateway entirely.

**Red team:** from the dev container, resolve external names (`getent hosts example.com`), then query a domain whose authoritative server you watch.  Try `TXT` and long labels to measure bandwidth.  Repeat on each runtime and version.

**When changing this code:** any change to the networks, or to `dns` settings in the compose file, must be retested against this vector.

### NET-4: Proxy bypass

**Severity:** High.  **Status:** Mitigated (N1).
**Where:** `internal: true` and the `dev` service's `networks` in [`docker-compose.yaml`](../../defaults/docker-compose.yaml).

The dev container has no route out except the gateway.  The proxy environment variables are only a convenience.  Everything else on this page assumes this holds.

**Red team:** connect directly to public and private addresses with the proxy variables unset, over TCP, UDP and ICMP, on IPv4 and IPv6.  Look for any other container or address reachable on the isolated network.

**When changing this code:** never add a second network to `dev`, `network_mode: host`, or published ports.

### NET-5: Allowed names resolving to forbidden addresses

**Severity:** Medium.  **Status:** Mitigated (N3, N4).
**Where:** the rule order in [`squid.conf`](../../containers/network-gateway/squid.conf); [`blocked-destinations.txt`](../../containers/network-gateway/blocked-destinations.txt); the `blocked_v4` set in [`egress.nft`](../../containers/firewall-router/egress.nft).

The gateway checks the hostname, then resolves it itself.  An allowed domain whose DNS an attacker controls can point anywhere.  Squid's blocked destination list and the firewall's blocked set keep it off private and special-purpose addresses.  The two lists are maintained separately and have already drifted: the gateway blocks `192.88.99.0/24`, and the firewall doesn't.

**Red team:** point an allowed name at private, loopback, link-local and metadata addresses, and at IPv6 addresses (`AAAA` records).  Try DNS rebinding between Squid's check and its connection.  Look for public-looking ranges that reach the host or LAN in your setup, such as a runtime that uses public address pools.

**When changing this code:** keep the domain check before the address check in `squid.conf`, so unlisted names are never resolved.  Keep the two blocked lists in sync.

### NET-6: Environments sharing a network

**Severity:** Medium.  **Status:** Unverified (N7).
**Where:** `docker: runs: compose-up` in [`setup.yaml`](../../defaults/setup.yaml); the resources directory in [`main_run.py`](../../scripts/main_run.py).

`compose-up` passes no project name, so compose derives one from the resources directory's name, `resources`, for every project.  Two environments running at once may share or replace each other's containers and networks.

**Red team:** start two environments from different projects.  From one, try to reach the other's `dev` container and gateway, and check whether either's containers were recreated.

**When changing this code:** give each environment a unique compose project name.

### NET-7: Traffic without firewall rules

**Severity:** Medium.  **Status:** Unverified (N6).
**Where:** the health check, `depends_on` and `restart` policies in [`docker-compose.yaml`](../../defaults/docker-compose.yaml); the firewall's [`entrypoint.sh`](../../containers/firewall-router/entrypoint.sh).

The gateway starts only after the firewall reports its rules loaded.  Both use `restart: unless-stopped`, and nothing tests what happens to the gateway, which lives in the firewall's network namespace, if the firewall restarts or its rules are deleted.

**Red team:** kill or restart the firewall container while the gateway runs, and check whether traffic still flows and through which rules.  Look for a window at startup.

**When changing this code:** the entrypoint must keep failing closed on anything unexpected, and the gateway must never start without the firewall's health check.

### NET-8: Reaching the host through the runtime

**Severity:** Medium.  **Status:** Unverified.
**Where:** the container runtime; see the limitations in the [containers README](../../containers/README.md).

Docker Desktop and Podman machine run containers inside a VM with their own networking, host aliases (`host.docker.internal`, `host.containers.internal`) and forwarding helpers.  Internal networks may behave differently there than on native Linux.

**Red team:** on each runtime, try the host aliases and the VM's gateway addresses from the dev container and from the gateway's namespace.

**When changing this code:** test network changes on every supported runtime, not only native Linux.

### NET-9: Forwarded ports

**Severity:** Medium.  **Status:** By design.
**Where:** `AllowTcpForwarding` and `PermitListen` in [`sshd_config`](../../containers/dev-env/sshd_config); the IDE's port settings.

A forwarded port lets the container serve content to the host's `localhost`, and to a browser pointed at it.  Remote forwarding (container to host) is refused by `PermitListen none`.

**Red team:** try remote forwarding from the container side.  Trigger an IDE's automatic port detection by printing "listening on" messages.  Serve hostile content on a forwarded port, aimed at the browser and at other services on the host's `localhost`.

**When changing this code:** keep `PermitListen none`, and consider `AllowTcpForwarding local`.

### NET-10: A subverted gateway

**Severity:** Medium.  **Status:** Mitigated (N4).
**Where:** [`network-gateway/Dockerfile`](../../containers/network-gateway/Dockerfile), [`egress.nft`](../../containers/firewall-router/egress.nft).

Squid parses hostile input.  If it's subverted, the firewall still limits it to TCP 443 on public IPv4 addresses, but not to the allowed domains.

**Red team:** check the Squid version for known issues, and try malformed `CONNECT` requests.  From inside the gateway's namespace, confirm what the firewall actually allows.

**When changing this code:** keep Squid unprivileged and without capabilities, and never give it `NET_ADMIN`.


## Sensitive code

Changes to these files can weaken the network properties, and need a security review:

* [`defaults/docker-compose.yaml`](../../defaults/docker-compose.yaml): networks, capabilities, start order.
* [`containers/firewall-router/`](../../containers/firewall-router/): the firewall rules and their start-up checks.
* [`containers/network-gateway/`](../../containers/network-gateway/): the proxy rules and the blocked destinations.
* [`containers/dev-env/sshd_config`](../../containers/dev-env/sshd_config): forwarding settings.
* The allowed domain handling in [`scripts/main_run.py`](../../scripts/main_run.py).
