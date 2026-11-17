# Envoy Gateway Attack Vectors

The weak points of the Envoy network gateway ([`containers/network-gateway-envoy`](../../containers/network-gateway-envoy/)), measured against the [network's required properties](network.md#required-properties).  The weak points every gateway shares are on the [network attack vectors](network-attack-vectors.md) page; the other gateways' are on the [Squid gateway](network-gateway-squid-attack-vectors.md) and [seekrit gateway](network-gateway-seekrit-attack-vectors.md) pages.  See the [attack vector overview](attack-vectors.md) for how to read the severity and status labels.

Envoy passes `tls` rules through, as Squid does, seeing only each `CONNECT`'s host and port, on any port the gateway's firewall lets out.  For an `https` rule's host, it reads the traffic: it ends the TLS connection with a certificate for that host, made for the run and signed by the run's CA, which the dev container trusts, checks each request's host and method, and makes its own TLS connection to the host.  Unlike the seekrit gateway, it never holds the CA's key, only a certificate for each host it reads, and it never fills in secrets.  It refuses `http` rules, `secrets`, and `https` wildcards.

For `tls` rules it uses Envoy's [dynamic forward proxy](https://www.envoyproxy.io/docs/envoy/v1.36.12/configuration/http/http_filters/dynamic_forward_proxy_filter), which resolves the host a request names and connects to it.  Envoy's documentation warns that such a proxy is "subject to confused deputy attacks", and that "any address reachable from the proxy is potentially accessible by untrusted clients".  Most of this page is about keeping that deputy to the rules.


## Attack surface

| Input | Controlled by | Handled in |
| --- | --- | --- |
| Egress rules | Your configuration (the project's suggestion, reviewed at `scoter pull`) | [`gateway_envoy.py`](../../scripts/gateway_envoy.py) turns them into Envoy's routes |
| `CONNECT` requests: the target, the `Host` header, other headers | Code in the dev container | Envoy's HTTP/1 codec and routes, as configured by [`gateway_envoy.py`](../../scripts/gateway_envoy.py) |
| Requests to read hosts: TLS, HTTP/1.1 and HTTP/2, `:authority`, methods, paths, headers, bodies | Code in the dev container | Each read host's internal listener and routes, in [`gateway_envoy.py`](../../scripts/gateway_envoy.py) |
| Responses from read hosts, and their certificates | The upstream servers | Envoy's upstream TLS, checked against the image's system CAs |
| The read hosts' certificates and keys | `network-gateway-mk-leaf`, with the run's CA | `make_leaves` in [`gateway_envoy.py`](../../scripts/gateway_envoy.py); the gateway's configuration file |
| DNS answers for allowed names | Whoever runs those domains' DNS | [`egress.nft`](../../containers/firewall-router/egress.nft) only |
| The run's CA | `scoter run` | [`gateway_start.py`](../../scripts/surf_scoter_lib/gateway_start.py), [`docker-compose.yaml`](../../defaults/docker-compose.yaml), `make_leaves` in [`gateway_envoy.py`](../../scripts/gateway_envoy.py) |
| The gateway's image | Envoy's upstream | [`network-gateway-envoy/Dockerfile`](../../containers/network-gateway-envoy/Dockerfile), pinned by digest |


## Summary

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [ENVOY-1](#envoy-1-the-host-checked-isnt-the-host-dialled) | The host checked isn't the host dialled | High | Mitigated |
| [ENVOY-2](#envoy-2-allowed-names-resolving-to-forbidden-addresses) | Allowed names resolving to forbidden addresses | Medium | Partly mitigated |
| [ENVOY-3](#envoy-3-dns-lookups-for-names-no-rule-allows) | DNS lookups for names no rule allows | Medium | Mitigated |
| [ENVOY-4](#envoy-4-a-subverted-envoy) | A subverted Envoy | Medium | Mitigated |
| [ENVOY-5](#envoy-5-the-runs-ca-key-kept-from-the-gateway) | The run's CA key, kept from the gateway | Low | Mitigated |
| [ENVOY-6](#envoy-6-a-read-hosts-requests-sent-somewhere-else) | A read host's requests sent somewhere else | High | Mitigated |
| [ENVOY-7](#envoy-7-the-read-hosts-certificate-keys) | The read hosts' certificate keys | Medium | Mitigated |
| [ENVOY-8](#envoy-8-the-gateways-memory-held-by-slow-readers) | The gateway's memory, held by slow readers | Low | Mitigated |
| [ENVOY-9](#envoy-9-what-the-log-holds) | What the log holds | Low | By design |


## Vectors

### ENVOY-1: The host checked isn't the host dialled

**Severity:** High.  **Status:** Mitigated (N2).
**Where:** `config`, `route` and `authority_match` in [`gateway_envoy.py`](../../scripts/gateway_envoy.py), and the invariant tests in [`test_gateway_envoy.py`](../../scripts/tests/test_gateway_envoy.py).

The confused deputy.  The routes decide whether a request is allowed; the dynamic forward proxy decides where it goes.  If those two see different hosts, the container can pass the check with an allowed host and be connected to another.  The dynamic forward proxy takes its host from filter state (`envoy.upstream.dynamic_host` and `dynamic_port`, when `allow_dynamic_host_from_filter_state` is on), then from a route's host rewrite (`host_rewrite_literal`, or `host_rewrite_header`, a header the client picks), and only then from the request's `:authority`.  Port stripping in the connection manager, or a virtual host's domain match that ignores the port, would let the check pass a port the rule didn't name.

The configuration has none of these.  The HTTP filters are only the dynamic forward proxy and the router, so nothing sets filter state.  No route rewrites the host.  There's one virtual host for every name, and each rule is a route matching the whole `:authority`, port included, in any case: `host:port` exactly, or, for `*.domain`, `(?i)^[a-z0-9-]+(\.[a-z0-9-]+)*\.domain:PORT$` in RE2.  For a `CONNECT`, Envoy takes the `:authority` from the request target, not the `Host` header.  Tested against Envoy 1.36.12:

* a `Host` header naming an allowed host doesn't let a `CONNECT` to another through;
* a `Host` header naming another host doesn't redirect an allowed `CONNECT`;
* a target with user information (`user@host:443`, `host:443@evil:443`) or without a port is refused (400);
* a trailing dot, a port no rule names, the bare domain of a wildcard, and plain `GET` requests get a 404.

**Red team:** try `CONNECT` targets with odd encodings, percent-escapes, IPv6 brackets, whitespace, duplicate or conflicting `Host` headers, absolute-form requests, HTTP/2 prior knowledge, and `Upgrade` headers.  Look for any way to make the upstream address differ from the matched route's.

**When changing this code:** never add `host_rewrite_*`, `auto_host_rewrite`, `allow_dynamic_host_from_filter_state`, a `set_filter_state` filter, an SNI or UDP dynamic forward proxy, `strip_any_host_port`, `strip_matching_host_port`, or `strip_trailing_host_dot`.  Keep the route matching the whole `:authority`, and the codec HTTP/1 only.  [`test_gateway_envoy.py`](../../scripts/tests/test_gateway_envoy.py) checks these; a change that breaks one of its invariant tests needs a security review, not a test update.

### ENVOY-2: Allowed names resolving to forbidden addresses

**Severity:** Medium.  **Status:** Partly mitigated (N3, N4).
**Where:** `dns_cache_config` in [`gateway_envoy.py`](../../scripts/gateway_envoy.py); the `blocked_v4` set and the loopback rule in [`egress.nft`](../../containers/firewall-router/egress.nft).

As with [SQUID-1](network-gateway-squid-attack-vectors.md#squid-1-allowed-names-resolving-to-forbidden-addresses), an allowed name whose DNS an attacker controls can point anywhere.  Unlike Squid and seekrit-proxy, Envoy's dynamic forward proxy has no list of addresses it refuses: it connects to whatever the name resolves to.  The firewall's blocked set still drops private, link-local (metadata), carrier-grade NAT, multicast, documentation and reserved IPv4 addresses, and it drops IPv6 entirely; Envoy looks up IPv4 addresses only.

What's left is loopback.  The firewall lets the gateway's namespace talk to itself (`oifname "lo" accept`), for Docker's DNS resolver, so a name resolving to `127.0.0.1` reaches whatever listens there, inside the gateway's namespace.  That's only Envoy's own listener, which takes the request through the same rules again.  There's no admin interface to reach.

**Red team:** point an allowed name at loopback, private, link-local, metadata and IPv4-mapped IPv6 addresses, and try rebinding between lookups.  From inside the gateway's namespace, list what listens on loopback.

**When changing this code:** never add Envoy's `admin` interface, or a second listener, without first closing loopback in the firewall (allowing only the DNS resolver's address there).  Keep `dns_lookup_family: V4_ONLY` while the firewall drops IPv6.

### ENVOY-3: DNS lookups for names no rule allows

**Severity:** Medium.  **Status:** Mitigated (N2).
**Where:** the filter list and routes in [`gateway_envoy.py`](../../scripts/gateway_envoy.py).

A lookup is itself a way out: a request for `<data>.attacker.example` carries the data to the attacker's DNS server, whatever the gateway does next.  The dynamic forward proxy filter only looks up a host when the request's route sends it to the proxy's cluster, and only a rule's route does.  A request no rule matches reaches the router without a route and gets a 404 without a lookup.  This is the same property Squid gets from checking the name before the address.

A wildcard rule still allows lookups of any name under its domain, and the cache holds at most 1024 names, with at most 256 lookups waiting.

**Red team:** with a packet capture on the uplink, send `CONNECT`s for unlisted names, names that differ from an allowed one only in case or a trailing dot, and plain requests, and confirm no DNS query leaves.

**When changing this code:** keep the dynamic forward proxy behind the routes; never give a route that matches anything broader than a rule the proxy's cluster.

### ENVOY-4: A subverted Envoy

**Severity:** Medium.  **Status:** Mitigated (N4).
**Where:** [`network-gateway-envoy/Dockerfile`](../../containers/network-gateway-envoy/Dockerfile), [`egress.nft`](../../containers/firewall-router/egress.nft).

Envoy parses hostile input.  If it's subverted, the firewall still limits it to the ports it lets out (TCP 443) on public IPv4 addresses, but not to the allowed hosts.  It never holds a secret, or the run's CA key ([ENVOY-5](#envoy-5-the-runs-ca-key-kept-from-the-gateway)).  It can see where data goes; it can read the traffic to the hosts it reads, and pass as them to the dev container ([ENVOY-7](#envoy-7-the-read-hosts-certificate-keys)), but no others.  The image is Envoy's distroless one, with no shell, pinned by digest.  Envoy runs as an unprivileged user, without capabilities, with two worker threads and limits on connections, header size and DNS lookups.

**Red team:** check the Envoy version for known issues, and try malformed and oversized `CONNECT` requests and slow headers.  From inside the gateway's namespace, confirm what the firewall actually allows.

**When changing this code:** keep Envoy unprivileged and without capabilities, never give it `NET_ADMIN`, and update the pinned image, digest and tag together.

### ENVOY-5: The run's CA key, kept from the gateway

**Severity:** Low.  **Status:** Mitigated.
**Where:** as [SQUID-3](network-gateway-squid-attack-vectors.md#squid-3-the-runs-ca-with-a-gateway-that-doesnt-use-it); `make_leaves` in [`gateway_envoy.py`](../../scripts/gateway_envoy.py).

The dev container trusts the run's CA for every host.  Envoy never gets its key: with `network-gateway-reads-tls: 'no'`, its `/user-space/ca` is an empty directory, and the key stays on the host, readable only by you, until the run ends.  The key is only used on the host, by `network-gateway-write-config`, to sign a certificate for each host the rules read ([ENVOY-7](#envoy-7-the-read-hosts-certificate-keys)).  So a subverted Envoy can pass as the read hosts, but not as any other.

**Red team:** as SQUID-3.  Also check that the gateway's configuration holds no CA key, and no certificate for a host its rules don't read, or for a wildcard, or that's a CA itself.

**When changing this code:** keep Envoy's `network-gateway-reads-tls` at `'no'`.  Keep each certificate to one exact host, a server certificate, not a CA.

### ENVOY-6: A read host's requests sent somewhere else

**Severity:** High.  **Status:** Mitigated (N2).
**Where:** `_intercept_listener`, `_intercept_clusters`, `request_authority_regex` and `_overlap_refusal` in [`gateway_envoy.py`](../../scripts/gateway_envoy.py), and the invariant tests in [`test_gateway_envoy.py`](../../scripts/tests/test_gateway_envoy.py).

The confused deputy again, inside TLS.  Once the gateway reads a host's traffic, the request itself names a host (`:authority`, or HTTP/1's `Host`), which the container chooses, and could aim at another site behind the same server, or the same CDN.  The gateway could also be made to talk to a server that isn't the host, if it didn't check the server's certificate.

Each read host and port has its own internal listener, reached only from the `CONNECT` route for that host and port, whatever TLS server name the client then sends; it always presents that host's certificate.  It sends a request on only when its `:authority` is that host (its port optional on 443), in any case, and its method is one the rules allow; anything else gets a 403 from the gateway.  It sends it to a cluster that names the host, fixed, resolved by DNS, not to anything the request says, over TLS with the host as its server name (SNI), checking the server's certificate against the image's system CAs and the host's DNS name.  The gateway decides whether to read or pass through from the `CONNECT`, so a host and port that one rule reads and another (exact or a wildcard) passes through is refused, rather than letting the tunnel bypass the methods.  Tested against Envoy 1.36.12: a `POST` where the rule allows `GET`, and a request naming another host on an allowed host's connection, get a 403; a host whose certificate is for another name, or expired, gets a 503; HTTP/1.1 and HTTP/2 both work.

**Red team:** send requests with a `Host` and `:authority` that differ, absolute-form request targets, duplicate `Host` headers, a trailing dot, odd case or percent-escapes in the host, `CONNECT` and extended `CONNECT` (WebSocket) inside the tunnel, HTTP/2 connection reuse across hosts, and a TLS server name for another host.  Point a read host's DNS at a server with a certificate for another name.

**When changing this code:** keep one internal listener per read host and port, the `:authority` and method matches on its route, the refusal as the last route, and the upstream fixed to the host, with `sni`, `trusted_ca` and the DNS name match.  Never add `trust_chain_verification: ACCEPT_UNTRUSTED`, a host rewrite, or the dynamic forward proxy to a read host's path.

### ENVOY-7: The read hosts' certificate keys

**Severity:** Medium.  **Status:** Mitigated.
**Where:** `make_leaves` and `main` in [`gateway_envoy.py`](../../scripts/gateway_envoy.py); `dev-env: runs: network-gateway-mk-leaf` in [`setup.yaml`](../../defaults/setup.yaml); the gateway's configuration in the run's resources directory.

Each read host's certificate key is in the gateway's configuration, which the gateway runs as its own user to read, so it's readable by others (mode 644).  Whoever can read it can pass as that host to the dev container while it trusts the run's CA.  The script refuses to write it unless a directory above it is yours alone, as `scoter run` does for the seekrit gateway's CA key.  The certificates are new each run, made in a private temporary directory that's removed, valid for 30 days, and only good for as long as the dev container trusts the CA, which is new each run.

**Red team:** look for the keys outside the configuration: in the temporary directory, in the run's log, in `write-config`'s output.  Check the configuration's directory and its parents during a run.

**When changing this code:** keep the keys out of everything but the configuration, and keep the private-directory check.

### ENVOY-8: The gateway's memory, held by slow readers

**Severity:** Low.  **Status:** Mitigated.
**Where:** `HTTP2_WINDOWS` and `MAX_CONCURRENT_STREAMS` in [`gateway_envoy.py`](../../scripts/gateway_envoy.py); `mem_limit` in [`docker-compose.yaml`](../../defaults/docker-compose.yaml).

Reading a host puts Envoy between two connections.  With Envoy's default HTTP/2 stream window (256 MiB), an upstream sends a whole response into the gateway's memory however slowly the container reads it: measured, 20 slow readers held 160 MB.  The windows are set to 1 MiB a stream and 4 MiB a connection, both ways, with at most 100 streams on a connection from the container; 20 slow readers then hold about 30 MB.  The container can still use up the gateway's memory with enough slow connections, which stops its own network until the gateway restarts; nothing else.

**Red team:** open many connections and streams to read hosts and read slowly; upload slowly to a slow server.  Watch the gateway's memory.

**When changing this code:** keep the windows small; Envoy's defaults are for trusted networks.

### ENVOY-9: What the log holds

**Severity:** Low.  **Status:** By design.
**Where:** `ACCESS_LOG_FORMAT` in [`gateway_envoy.py`](../../scripts/gateway_envoy.py); [`netlog.py`](../../scripts/surf_scoter_lib/netlog.py).

For a read host, the access log has each request's method and path, which may carry a token in its query string, and `scoter netlog` shows them.  The log goes to the container tool's log for the gateway, readable by whoever can use the container tool.  It never has headers or bodies.

**Red team:** check what a request with a token in its path leaves in the log, and where.

**When changing this code:** never log headers or bodies.


## Sensitive code

Changes to these files can weaken the Envoy gateway, and need a security review:

* [`containers/network-gateway-envoy/`](../../containers/network-gateway-envoy/): the image, and its default configuration.
* [`scripts/gateway_envoy.py`](../../scripts/gateway_envoy.py): what Envoy is told to allow, and how; see [ENVOY-1](#envoy-1-the-host-checked-isnt-the-host-dialled).
* [`scripts/tests/test_gateway_envoy.py`](../../scripts/tests/test_gateway_envoy.py): the invariant tests.
* [`scripts/surf_scoter_lib/gateway_start.py`](../../scripts/surf_scoter_lib/gateway_start.py): the run's CA, and which gateway gets it.
* `dev-env: runs: network-gateway-mk-leaf` in [`defaults/setup.yaml`](../../defaults/setup.yaml): the read hosts' certificates.
