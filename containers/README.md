# Domain-aware CONNECT proxy with separate nftables firewall service

## Layout

- `dev`: isolated client, no uplink network.
- `gateway`: unprivileged Squid; **shares the firewall service network namespace** (`network_mode: service:firewall`).
- `firewall`: owns both networks and installs nftables input/output/forward rules. Only this container has `NET_ADMIN`.

This is a separate *firewall process/container*, not an independent routed hop or independently isolated firewall namespace. Namespace sharing is deliberate: Docker Compose cannot portably assign an arbitrary container default route, and the shared namespace makes nftables an unavoidable kernel egress filter for Squid without adding NET_ADMIN to Squid. Do not give Squid privileged mode or the Docker socket. A compromised Docker daemon/host can bypass the controls.

## Start and test

```sh
docker compose up -d --build
docker compose exec dev curl -vI --max-time 15 https://github.com
docker compose exec dev curl -vI --max-time 15 https://example.com
docker compose exec dev curl -vI --noproxy '*' --connect-timeout 3 https://github.com
docker compose exec firewall nft list table inet egress
docker compose logs gateway
```

Expected: approved domain works (subject to service availability); example.com is denied by Squid (403); direct client connection fails; firewall logs/counters show drops. `nft` counters can be inspected using `nft -a list table inet egress`.

## Policy

- Squid allows only CONNECT to port 443 and only listed destination domains.
- nftables permits Squid's outgoing IPv4 TCP/443 to public addresses; blocks private, loopback, link-local, CGNAT, multicast, reserved/documentation ranges; drops IPv6 and all other outbound traffic except DNS via Docker's embedded resolver.
- Inbound proxy port 3128 is accepted only from the isolated subnet; there is no published host port.
- No TLS interception or private CA required.

## Limitations / hardening

1. This sample assumes Docker Engine / Docker Desktop running Linux containers with `NET_ADMIN` and nftables available. Docker Desktop implementations differ: validate on each host OS. It was **not runtime-tested here**.
2. Docker's embedded DNS uses 127.0.0.11, and Docker may implement DNS forwarding with host-side NAT. Test whether DNS traffic traverses the expected hooks. For strong DNS control use a separately enforced resolver.
3. The output firewall is **IP/port filtering**, not hostname validation. If Squid is compromised it could connect to arbitrary public TCP/443; the domain allowlist depends on Squid. Restrict Squid admin/config permissions and test against DNS rebinding.
4. IPv6 is disabled at nftables output/input by policy; verify with IPv6-enabled Docker hosts.
5. If a developer has Docker daemon privileges they can bypass this design. Restrict daemon access where that matters.
6. Docker `internal` networks do not by themselves prevent every possible host/container interaction. Apply host-level policies if necessary.
7. `nft` rules are applied at startup. A process that already has network access before policy installation is a concern; the Squid service waits for firewall health, but the firewall container itself briefly exists before policy application.
8. Use immutable image digests, resource limits, a read-only filesystem, and vulnerability scanning for production.
9. Some JetBrains features require SSH forwarding; assess whether that creates an unintended path to host services.
10. On Docker Desktop, Docker's embedded DNS behavior and port forwarding may differ from native Linux.
