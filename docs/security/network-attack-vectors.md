# Network Attack Vectors

The weak points of the [runtime network](network.md), measured against its [required properties](network.md#required-properties) (N1 to N8), whichever gateway it runs.  Each gateway's own weak points are on its page: the [Squid gateway](network-gateway-squid-attack-vectors.md) (`SQUID`, the default) the [seekrit gateway](network-gateway-seekrit-attack-vectors.md) (`SEEKRIT`), and the [Envoy gateway](network-gateway-envoy-attack-vectors.md) (`ENVOY`).  Contributors should treat changes to the code listed here as security-sensitive, and the red team can use each vector as a starting point.  See the [attack vector overview](attack-vectors.md) for how to read the severity and status labels.


## Attacker goals

Code in the dev container wants to:

* **exfiltrate** source code, credentials or anything else it can read;
* **move laterally** to the host's services, the LAN or VPN, or cloud metadata endpoints;
* **receive control** from outside, or offer a service that something on the host connects to.


## Attack surface

| Input | Controlled by | Handled in |
| --- | --- | --- |
| Egress rules | The project file's `egress`, as pulled into your configuration | The generator's checks ([`egress.rs`](../../rust/host/config/src/config/egress.rs)), then the gateway's script and container: see its page |
| Requests to the gateway, hostnames | Code in the dev container | The gateway: see its page |
| DNS answers for allowed domains | Whoever runs those domains' DNS | The gateway's address checks, and [`egress.nft`](../../containers/firewall-router/egress.nft) |
| DNS queries from the dev container | Code in the dev container | The container runtime's resolver |
| Network topology and start order | Your settings | [`docker-compose.yaml`](../../dist-files/defaults/docker-compose.yaml), [firewall `entrypoint.sh`](../../containers/firewall-router/entrypoint.sh) |
| Forwarded ports | You, through the IDE | [`sshd_config`](../../containers/dev-env/sshd_config), IDE settings |


## Summary

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [NET-1](#net-1-allowed-domains-are-open-channels) | Allowed domains are open channels | High | By design |
| [NET-2](#net-2-the-project-controls-the-allowlist) | The project controls the allowlist | High | Mitigated |
| [NET-3](#net-3-dns-from-the-dev-container) | DNS from the dev container | High | Unverified |
| [NET-4](#net-4-proxy-bypass) | Proxy bypass | High | Mitigated |
| [NET-5](#net-5-moved) | Moved to [SQUID-1](network-gateway-squid-attack-vectors.md#squid-1-allowed-names-resolving-to-forbidden-addresses) and [SEEKRIT-8](network-gateway-seekrit-attack-vectors.md#seekrit-8-allowed-names-resolving-to-forbidden-addresses) (and, since, [ENVOY-2](network-gateway-envoy-attack-vectors.md#envoy-2-allowed-names-resolving-to-forbidden-addresses)) | | Moved |
| [NET-6](#net-6-environments-sharing-a-network) | Environments sharing a network | Medium | Unverified |
| [NET-7](#net-7-traffic-without-firewall-rules) | Traffic without firewall rules | Medium | Unverified |
| [NET-8](#net-8-reaching-the-host-through-the-runtime) | Reaching the host through the runtime | Medium | Unverified |
| [NET-9](#net-9-forwarded-ports) | Forwarded ports | Medium | By design |
| [NET-10](#net-10-moved) | Moved to [SQUID-2](network-gateway-squid-attack-vectors.md#squid-2-a-subverted-squid) and [SEEKRIT-5](network-gateway-seekrit-attack-vectors.md#seekrit-5-a-subverted-gateway) (and, since, [ENVOY-4](network-gateway-envoy-attack-vectors.md#envoy-4-a-subverted-envoy)) | | Moved |

Image builds also have open network access; see [DEV-3](dev-container-attack-vectors.md#dev-3-image-builds-run-outside-the-isolation).


## Vectors

### NET-1: Allowed domains are open channels

**Severity:** High.  **Status:** By design.
**Where:** your configuration's egress rules; the gateway that enforces them.

Once a host is allowed, anything in the container can send it anything the rule lets through.  For a `tls` rule (the only kind Squid takes), the gateway sees the hostname but not the content.  Allowing `github.com` lets code push source to any repository it has credentials for, or post it as a gist.  Wildcards (`*.example.com`, Squid and Envoy only) widen this to every subdomain, including hosts that serve user content.  An `https` rule (the seekrit or Envoy gateway) narrows it to the rule's methods, but `GET` still carries data in its path and headers.  A rule with `secrets` adds your credentials to what the container can use ([SEEKRIT-1](network-gateway-seekrit-attack-vectors.md#seekrit-1-a-secret-used-through-the-host-its-rule-allows), [ENVOY-11](network-gateway-envoy-attack-vectors.md#envoy-11-a-secret-used-through-the-host-its-rule-allows)).

**Red team:** exfiltrate through each host in a typical set of rules (repository pushes, gists, package uploads, issue comments).  Look for allowed wildcards that cover user-controlled hosts, and for `https` rules whose methods still let data out.

**When changing this code:** the default rules should stay minimal.  A gateway that reads content changes the trust model (a CA the container trusts, plaintext in the gateway); the [seekrit gateway's page](network-gateway-seekrit-attack-vectors.md) is that analysis.

### NET-2: The project controls the allowlist

**Severity:** High.  **Status:** Mitigated by [project verification](verification.md).
**Where:** the project file's `egress`; the egress checks in [`egress.rs`](../../rust/host/config/src/config/egress.rs), [`public_suffix.rs`](../../rust/host/config/src/config/public_suffix.rs) and [`checks.py`](../../py/src/dist_bin/surf_scoter_lib/checks.py); the gateway's script ([`gateway_squid.py`](../../py/src/dist_bin/gateway_squid.py), [`gateway_seekrit.py`](../../py/src/dist_bin/gateway_seekrit.py), [`gateway_envoy.py`](../../py/src/dist_bin/gateway_envoy.py)); `_write_gateway_config` in [`main_run.py`](../../py/src/dist_bin/main_run.py).

The project suggests the rules, but the gateway gets only your configuration's rules, which you approved with `scoter pull`, from the verified store.  Without a configuration, nothing gets out.  The generator refuses hosts that aren't plain, lower-case DNS names (no IP addresses, a `*` only as a leading `*.`, at least two labels), wildcards over a public suffix, and `secrets` anywhere but on an `https` rule for an exact host, on a port in the setup's `network-gateway-tls-ports`.  The gateway's script writes each host into its own configuration (escaped into Squid's pattern or Envoy's route, or quoted in seekrit-proxy's), only on the ports in the setup's `network-gateway-ports`, and stops the launch on a rule the gateway can't enforce.

What's left:

* **The checks are thin.**  The review flags plain `http` and hosts on the `verification: egress-hosts` lists; any other host is shown, but only as a rule.  [NET-1](#net-1-allowed-domains-are-open-channels) is the real risk of each one.
* **The public suffix list is off by default.**  `verification: path-lists: public-suffix-lists` is empty, so only the built-in `egress-refused-wildcards` list stops a wildcard such as `*.some-hosting-provider.example`.
* **Approving without looking.**  A reviewer of your own can approve rules nobody looked at ([VER-27](verification-attack-vectors.md#ver-27-approval-cant-prove-a-person-reviewed)); an unlocked configuration runs edited rules unreviewed ([VER-33](verification-attack-vectors.md#ver-33-the-unlocked-state)).

**Red team:** ship a project whose rules contain very broad entries, such as `*.com`, a bare IP address, a wildcard over a hosting provider's user domains, or entries Squid might parse unexpectedly, and see what the review shows and the gateway allows.  Edit a rule while unlocked, and see what the run's warning shows.

**When changing this code:** validation belongs in the generator, before the rules reach the review, and again before they reach the gateway.  Keep the gateway's configuration made only from the configuration's rules.

### NET-3: DNS from the dev container

**Severity:** High, if the channel exists.  **Status:** Unverified (N8).
**Where:** the `isolated` network in [`docker-compose.yaml`](../../dist-files/defaults/docker-compose.yaml); the container runtime's resolver (Docker's embedded DNS, Podman's `aardvark-dns`).

The firewall limits the gateway's DNS, but the dev container has its own path to the runtime's resolver on the isolated network.  If that resolver forwards queries for external names, DNS becomes an exfiltration and command channel that bypasses the gateway entirely.

**Red team:** from the dev container, resolve external names (`getent hosts example.com`), then query a domain whose authoritative server you watch.  Try `TXT` and long labels to measure bandwidth.  Repeat on each runtime and version.

**When changing this code:** any change to the networks, or to `dns` settings in the compose file, must be retested against this vector.

### NET-4: Proxy bypass

**Severity:** High.  **Status:** Mitigated (N1).
**Where:** `internal: true` and the `dev` service's `networks` in [`docker-compose.yaml`](../../dist-files/defaults/docker-compose.yaml).

The dev container has no route out except the gateway.  The proxy environment variables are only a convenience.  Everything else on this page assumes this holds.

**Red team:** connect directly to public and private addresses with the proxy variables unset, over TCP, UDP and ICMP, on IPv4 and IPv6.  Look for any other container or address reachable on the isolated network.

**When changing this code:** never add a second network to `dev`, `network_mode: host`, or published ports.

### NET-5: Moved

**Status:** Moved.  Allowed names resolving to forbidden addresses depends on how the gateway resolves them: it's [SQUID-1](network-gateway-squid-attack-vectors.md#squid-1-allowed-names-resolving-to-forbidden-addresses), [SEEKRIT-8](network-gateway-seekrit-attack-vectors.md#seekrit-8-allowed-names-resolving-to-forbidden-addresses) and [ENVOY-2](network-gateway-envoy-attack-vectors.md#envoy-2-allowed-names-resolving-to-forbidden-addresses).  The ID isn't reused.

### NET-6: Environments sharing a network

**Severity:** Medium.  **Status:** Unverified (N7).
**Where:** `docker: runs: compose-up` in [`setup.yaml`](../../dist-files/setup.yaml); the resources directory in [`main_run.py`](../../py/src/dist_bin/main_run.py).

`compose-up` passes no project name, so compose derives one from the resources directory's name, `resources`, for every project.  Two environments running at once may share or replace each other's containers and networks.

**Red team:** start two environments from different projects.  From one, try to reach the other's `dev` container and gateway, and check whether either's containers were recreated.

**When changing this code:** give each environment a unique compose project name.

### NET-7: Traffic without firewall rules

**Severity:** Medium.  **Status:** Unverified (N6).
**Where:** the health check, `depends_on` and `restart` policies in [`docker-compose.yaml`](../../dist-files/defaults/docker-compose.yaml); the firewall's [`entrypoint.sh`](../../containers/firewall-router/entrypoint.sh).

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

### NET-10: Moved

**Status:** Moved.  What a subverted gateway holds, and so what it costs, depends on the gateway: it's [SQUID-2](network-gateway-squid-attack-vectors.md#squid-2-a-subverted-squid), [SEEKRIT-5](network-gateway-seekrit-attack-vectors.md#seekrit-5-a-subverted-gateway) and [ENVOY-4](network-gateway-envoy-attack-vectors.md#envoy-4-a-subverted-envoy).  Either way, the firewall limits it to TCP 443 on public IPv4 addresses (N4).  The ID isn't reused.


## Sensitive code

Changes to these files can weaken the network properties, and need a security review:

* [`dist-files/defaults/docker-compose.yaml`](../../dist-files/defaults/docker-compose.yaml): networks, capabilities, start order.
* [`containers/firewall-router/`](../../containers/firewall-router/): the firewall rules and their start-up checks.
* Each gateway's container and script: see the [Squid](network-gateway-squid-attack-vectors.md#sensitive-code), [seekrit](network-gateway-seekrit-attack-vectors.md#sensitive-code) and [Envoy](network-gateway-envoy-attack-vectors.md#sensitive-code) pages.
* [`containers/dev-env/sshd_config`](../../containers/dev-env/sshd_config): forwarding settings.
* The egress checks in [`rust/host/config/src/config/egress.rs`](../../rust/host/config/src/config/egress.rs) and [`py/src/dist_bin/surf_scoter_lib/gateway.py`](../../py/src/dist_bin/surf_scoter_lib/gateway.py), and the gateway handling in [`py/src/dist_bin/main_run.py`](../../py/src/dist_bin/main_run.py).
