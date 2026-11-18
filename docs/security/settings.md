# User and Default Settings

Much of what keeps an environment contained comes not from the project or from Surf Scoter's code, but from files Surf Scoter ships as defaults:

* the default system configuration, [`dist-files/setup.yaml`](../../dist-files/setup.yaml);
* the compose file that wires the containers together, [`dist-files/defaults/docker-compose.yaml`](../../dist-files/defaults/docker-compose.yaml);
* the firewall container, [`containers/firewall-router`](../../containers/firewall-router/);
* the gateway container: Squid, [`containers/network-gateway-squid`](../../containers/network-gateway-squid/), by default, or seekrit-proxy, [`containers/network-gateway-seekrit`](../../containers/network-gateway-seekrit/).

You can replace every one of them through your own [system configuration](../system-config-file.md).

**How to weigh these.**  Surf Scoter maintains and reviews the shipped defaults, but changes you make are yours to review.  These files come from you, not from a project you cloned, so they rank below project-supplied files as a target for analysis.  They still deserve their own scrutiny: together they provide almost every [required network property](network.md#required-properties).  A mistake in a project file affects that project.  A mistake here silently removes the protection for every project you run.


## Where settings come from

The [system configuration file](../system-config-file.md) describes the loading rules in full.  The parts that matter for security:

* Settings load from the installation's `setup.yaml` (lowest priority), then your own setup file (highest).  A higher priority file replaces individual keys.
* The tools **never** read setup files from a project.  `$SURF_SCOTER_HOME`, your setup file, and a `distribution-dir` taken from a setup file must all lie outside the project, its clone, and the repository holding it; otherwise the tools stop without making a configuration.  This matters because a project can set `SURF_SCOTER_HOME` itself, through tools such as direnv that run when you enter its directory.
* `scoter` reports when your setup weakens one of the *guarded* settings, such as removing a default check, turning off the review notice, or moving the unlocked directory, and `scoter run` refuses to start when a setup program resolves into the project or through a relative path.  See [guarded settings](../system-config-file.md#guarded-settings).
* A setup file can run any program on your computer: every `runs` entry is a command line, and every `programs` entry picks an executable.  It also decides what a project may do, such as the project files directory and where dev containers may come from.  Treat a setup file like a shell script.


## The system configuration

These keys from [`dist-files/setup.yaml`](../../dist-files/setup.yaml) carry the most security weight.

| Key | What it does | What to keep in mind when changing it |
| --- | --- | --- |
| `docker: programs: exec` and `docker: runs: *` | Build images, start and stop the environment, start the bridge broker | These run on your host with your privileges.  `broker` must not request a TTY, and must run inside the named container. |
| `ssh: runs: proxy-command` | How SSH reaches the container | Keep it going through the runtime's exec channel.  Switching to a published port breaks N5. |
| `ssh: paths: key-file`, `known-hosts`, `config-file` | Your client key and the files `scoter` edits | `scoter` rewrites the config and `known_hosts` files on each run. |
| `project-state: paths: *` | Where the project clone, setup and resources directories live | Keep all three, and the directories above them, where the container can't write: the clone is mounted read-write, so nothing may be inside it, and a directory the container could replace with a symlink would redirect where `scoter` clones and deletes ([FILE-10](files-attack-vectors.md#file-10-the-clones-path-through-container-symlinks)).  The default keeps them outside the project, in `state-dir` (FILE-9). |
| `project-state: paths: secret-store` | Your secret store for the project: the values of the secrets its egress rules name | Anyone who can change this path chooses which values the gateway sends, and the store's `command` entries run on your host.  Keep it out of every project and its clone, readable only by you, and never mounted.  Guarded, with the other `project-state` paths. |
| `project-state: values: persistent-store-volume`, `persistent-store-prefix` | The dev container's persistent store, and how `scoter cleanup` recognizes the tools' volumes | Keep the name different for each project, or two projects' containers share one store.  A prefix that matches other volumes lets `scoter cleanup --persistent-stores` remove them. |
| `git-vcs: runs: *`, `file-vcs: runs: *` | How the project clone is created, refreshed and merged back | None of them may run with the clone as its working directory or repository, or push into it: Git would run the clone's hooks on your host ([FILE-1](files-attack-vectors.md#file-1-scoter-run-runs-git-inside-the-project-clone)).  `refresh-bundle`, `set-clone-origin` and `fetch-changes-into` are guarded. |
| `docker: runs: inspect-volume`, `create-volume`, `remove-volume`, `list-volumes` | The persistent store volume | `inspect-volume` must fail only when the volume isn't there, or `scoter run` tries to create it again, which fails with Podman. |
| `dev-env: runs: network-gateway-write-config` | Turns the egress rules into the gateway's configuration | A command that writes a broader list than the rules say breaks N2. |
| `dev-env: values: network-gateway-name` | The gateway the egress rules are enforced by | Part of the setup a pull records; a gateway that enforces less than its name suggests weakens N2. |
| `dev-env: values: network-gateway-reads-tls` | Whether the gateway is given the run's CA key | `'yes'` only for a gateway that looks inside TLS: any gateway given the key can intercept the dev container's TLS to any host.  With it, the key is readable by others, so keep the resources directory inside one only you can use. |
| `dev-env: runs: network-gateway-mk-ca` | Makes each run's CA, which the dev container trusts | A CA that's reused, or whose key others can read, lets anyone with it intercept the dev container's TLS to any host.  Keep it new for each run, with its key readable only by you. |
| `dev-env: runs: network-gateway-mk-leaf` | Makes a certificate for each host the Envoy gateway reads, signed by the run's CA | A certificate for a host the rules don't name, or for a wildcard or a CA, lets the gateway's holder intercept more than the rules say.  Keep it to the one host it's given (`{x:subject}`, `{x:san}`), a server certificate, not a CA. |
| `dev-env: runs: network-gateway-post-start`, `docker: runs: exec-stdin` | Give the gateway what it needs once running: the seekrit gateway's secrets | They handle your secret values.  `exec-stdin` must pass them on standard input, without a TTY, and to the gateway's container only. |
| `dev-env: value-lists: network-gateway-ports`, `network-gateway-tls-ports` | The ports egress rules may use, and of those, where `https` rules (and their secrets) are allowed | Keep `network-gateway-ports` the same as the firewall's: a port the firewall drops gives a rule that fails, and opening a port in the firewall without listing it here leaves it unused.  A port in `network-gateway-tls-ports` lets the gateway read, and inject secrets into, traffic on it, so list only ports where the upstream speaks TLS.  Both are guarded. |
| `dev-env: paths: docker-compose`, `firewall-router`, `network-gateway` | Which compose file and network containers to use | Replacing these replaces the network.  Recheck every property. |
| `dev-env: values: *-image` | Image names for the containers | An image from a registry rather than a local build means trusting whoever publishes it. |
| `dev-env: values: mem-limit`, `cpus`, `pids-limit`, `logging-max-size` | Dev container resource limits | Removing them lets the container exhaust your host. |
| `verification: host-commands`, `project-files`, `egress-hosts`, `value-lists: egress-refused-wildcards`, `path-lists: public-suffix-lists` | The checks on a configuration | They run at every pull and every run, so weakening them weakens every configuration at once, and strengthening them can stop a run that worked ([VER-6](verification-attack-vectors.md#ver-6-setup-changes-that-dont-ask-for-a-new-review), [VER-22](verification-attack-vectors.md#ver-22-checks-on-every-run-can-stop-a-run-that-used-to-work)). |
| `verification: paths: store` | Where your configurations are kept | Keep it outside every project and its clone, private to you, and never mounted into a container ([VER-8](verification-attack-vectors.md#ver-8-the-store-itself)). |
| `verification: values: review-notice-first-days`, `review-notice-every-days`, `review-warn-days`, `review-expire-days` | The review timers | Turning the notice off, or lengthening it, lets a configuration outlive the reasons it was approved; the notice settings are guarded ([VER-20](verification-attack-vectors.md#ver-20-trust-doesnt-expire)). |
| `pull: runs: reviewer` | The program that shows a pull for review | A reviewer that renders project text, or approves on its own, undoes the review ([VER-26](verification-attack-vectors.md#ver-26-the-reviewer-is-a-setup-program), [VER-27](verification-attack-vectors.md#ver-27-approval-cant-prove-a-person-reviewed)).  Guarded. |
| `pull: paths: unlocked-dir`, `values: unlock-reminder-days`, `unlock-days` | Where an unlocked configuration runs from, and the reminders while it does | Refused inside a project, its clone, a state directory or the store; the container could otherwise write the next run's settings ([VER-37](verification-attack-vectors.md#ver-37-the-unlocked-directory-setting), [VER-33](verification-attack-vectors.md#ver-33-the-unlocked-state)).  Guarded. |
| `pull: paths: templates-dir` | The templates `scoter init` starts from | Refused inside a project or its repository ([VER-7](verification-attack-vectors.md#ver-7-a-distribution-inside-a-project)); a template is reviewed by `scoter lock` either way.  Guarded. |
| `verification: values: max-file-size`, `max-snapshot-size` | Limits on what a pull reads and a configuration keeps | A project over the limits can't be pulled; raising them costs disk space, and lets seeding read more before it refuses ([VER-23](verification-attack-vectors.md#ver-23-seeding-reads-hostile-input)). |
| `scoter: path-lists: dev-containers-search` | Where containers are found by name, after the configuration's own | A container found here is trusted, not reviewed.  A directory inside a project, its repository or its clone is refused ([VER-9](verification-attack-vectors.md#ver-9-trusted-containers), [VER-24](verification-attack-vectors.md#ver-24-a-lookup-reaches-the-live-project)). |


## The compose file

[`dist-files/defaults/docker-compose.yaml`](../../dist-files/defaults/docker-compose.yaml) is where the network properties are actually wired up.  `scoter run` copies it into the environment's resources directory and fills it in from a generated `.env` file.  Some of those values come from the project's `dev-env.yaml`; see [dev container attack vectors](dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file).

| Setting | Property |
| --- | --- |
| `isolated` network is `internal: true` | N1: no route out from the isolated network |
| `dev` is attached only to `isolated` | N1 |
| `firewall` is on both networks, with the alias `gateway` on `isolated` | N1, N2: the only bridge between the two networks |
| `gateway` uses `network_mode: service:firewall` | N4: every gateway connection passes the firewall's rules |
| `cap_drop: [ALL]` everywhere; `cap_add: [NET_ADMIN]` only on `firewall` | N4: only the firewall can change network rules |
| `no-new-privileges` everywhere | Programs can't raise their own privileges |
| Firewall health check, and `gateway` waits for `service_healthy` | N6: the proxy starts only once the rules are loaded |
| No `ports:` on any service | N5 |
| Setup directory mounted read-only, gateway configuration not mounted in the dev container | N2: the container can't change its own allowed hosts |
| The run's CA: the certificate mounted read-only in `dev`, the key only in a gateway that reads TLS | Only that gateway can sign certificates the dev container trusts ([SQUID-3](network-gateway-squid-attack-vectors.md#squid-3-the-runs-ca-with-a-gateway-that-doesnt-use-it), [SEEKRIT-3](network-gateway-seekrit-attack-vectors.md#seekrit-3-the-interception-ca)) |
| A tmpfs at the gateway's `/run/seekrit` | The seekrit gateway's secrets arrive through a socket there, never a file ([SEEKRIT-2](network-gateway-seekrit-attack-vectors.md#seekrit-2-secrets-in-the-gateways-memory-and-their-delivery)) |
| No fixed subnet on `isolated` | N7: each environment gets its own subnet |
| Process, memory, CPU and log limits | Contained resource use |

Some settings have caveats:

* `userns_mode: keep-id` works only with Podman, and Docker users must remove it.  With rootful Docker, container uid 1000 is host uid 1000, which may belong to a different user than you.  With rootless Docker, container root maps to you.  Either way, the ownership of the clone's files changes.
* The proxy variables in `environment` are a convenience.  The protection comes from `internal: true`.

**Never add** any of these to a service: `privileged: true`, a mount of the container runtime's socket, `network_mode: host`, `pid: host`, `devices`, extra networks on `dev`, published `ports`, additional `cap_add` on `dev` or `gateway`, or `security_opt` entries that turn off seccomp or AppArmor.


## The firewall container

[`containers/firewall-router`](../../containers/firewall-router/) is a small Alpine image with `nftables`.  Its only job is to own the network namespace and load the rules.  It provides N3, N4 and N6.

At startup, [`entrypoint.sh`](../../containers/firewall-router/entrypoint.sh) finds the uplink interface from the default route, takes the one remaining interface as the isolated network, and reads the resolver addresses from `/etc/resolv.conf`.  It stops with an error rather than guess if there's no default route, more than one candidate interface, or no IPv4 resolver.  It then replaces its own `inet egress` table, leaving the runtime's tables alone, loads [`egress.nft`](../../containers/firewall-router/egress.nft), and sleeps.

| Direction | Traffic | Action |
| --- | --- | --- |
| Out | Loopback, and replies to allowed connections | Allow |
| Out | Any IPv6 | Drop |
| Out | DNS (UDP and TCP 53) to the runtime's resolvers only | Allow |
| Out | Private and special-purpose IPv4 ranges | Drop |
| Out | TCP 443 to anything else | Allow |
| Out | Everything else, including UDP 443 (QUIC) | Count and drop |
| In | TCP 3128 from the isolated network interface | Allow |
| In | Everything else | Count and drop |
| Forward | Everything | Drop |

You can see the counters with `podman exec HOSTNAME-fr nft -a list table inet egress`.

When changing it, keep these properties:

* Every chain's policy stays `drop`.
* IPv6 stays dropped.  The blocked address set covers IPv4 only.
* DNS goes only to the runtime's resolvers.
* The blocked set matches the gateway's blocked destination list.
* The blocked set assumes your container runtime gives its networks private addresses.  If your runtime's address pools use public ranges, add them.


## The gateway container

The setup chooses the gateway: `dev-env: paths: network-gateway`, `values: network-gateway-image`, `network-gateway-name`, `network-gateway-config-mount` and `network-gateway-reads-tls`, and `runs: network-gateway-write-config` and `network-gateway-post-start`, together.  The distribution's [`setup.yaml`](../../dist-files/setup.yaml) has Squid's, and the seekrit and Envoy gateways' to copy into your setup.  Either way, the setup's `dev-env: value-lists: network-gateway-ports` must match the ports the firewall lets out.

### Gateway: Squid

[`containers/network-gateway-squid`](../../containers/network-gateway-squid/) runs Squid on Debian as the unprivileged `proxy` user, with no capabilities, caching off, and the access log on standard output (`podman logs HOSTNAME-ng`).  It provides N2, and backs up N3.

[`squid.conf`](../../containers/network-gateway-squid/squid.conf) applies its rules in this order:

1. Refuse anything that isn't `CONNECT`.
2. Refuse any port but 443.
3. Refuse any hostname not on the allowed host list (`/user-space/egress-hosts.txt`, made from the egress rules and mounted read-only).  Each line is a regular expression (`dstdom_regex`): `^host$` for a host, `^.+\.domain$` for `*.domain` (its subdomains, not the domain itself).  `-i` ignores case, and `-n` stops Squid from making reverse DNS lookups to test the list.
4. Refuse destinations that resolve into the [blocked destination list](../../containers/network-gateway-squid/blocked-destinations.txt): private, loopback, link-local, carrier-grade NAT, multicast, documentation and reserved ranges, for IPv4 and IPv6.
5. Allow what's left.

The order matters.  The hostname check comes before the address check, so an unlisted name is refused before Squid resolves it, and a request can't make Squid send a DNS query for an attacker's domain.

When changing it, keep these properties:

* The rule order stays as it is, and there's no `http_access allow all`.
* Only port 443 is allowed.
* Squid stays unprivileged and without capabilities.
* The blocked destination list stays in the image, out of reach of the user and the project.
* There's no TLS interception.  For that, use the seekrit gateway, whose analysis is its [attack vectors page](network-gateway-seekrit-attack-vectors.md).
* Its `network-gateway-reads-tls` stays `'no'`, so it's never given the run's CA key.

Its own weak points are on the [Squid gateway attack vectors](network-gateway-squid-attack-vectors.md) page.

### Gateway: seekrit

[`containers/network-gateway-seekrit`](../../containers/network-gateway-seekrit/) builds [seekrit-proxy](https://github.com/seekritdev/proxy) from a pinned upstream commit, with Surf Scoter's [patches](../../containers/network-gateway-seekrit/patches/), into a `scratch` image: the binary, and nothing else, not even a shell.  It runs as an unprivileged user (65532), with no capabilities, logs JSON to standard output (`scoter netlog` reads it), and stops on `SIGINT`.  It provides N2, backs up N3, and reads the hosts with `https` rules.

[`gateway_seekrit.py`](../../py/src/dist_bin/gateway_seekrit.py) writes its configuration (`/user-space/gateway/seekrit-proxy.toml`):

1. Refuse any host without a rule (`unmatched_host_policy = "deny"`).
2. A `tls` rule's host is passed through untouched (`intercept = false`), on its rules' ports.
3. An `https` or `http` rule's host is read, on its rule's port, with its methods; an `https` rule's `secrets` may be filled in (`allow`).
4. Never put a secret into plain HTTP (`plaintext_injection = "refuse"`).
5. Refuse destinations that resolve to private, loopback, link-local or special-purpose addresses (`[egress]`), connecting only to the address checked.
6. Scrub the values it put into a request out of that request's response (`[redaction]`).
7. Take the secrets after starting, once, over a socket in `/run/seekrit` (`[secrets] source = "inject"`).

It refuses wildcard hosts, and one host with rules it can't tell apart.  The run's CA is at `/user-space/ca`, with `network-gateway-reads-tls: 'yes'`.  Once it starts, `network-gateway-post-start` ([`gateway_postrun_seekrit.py`](../../py/src/dist_bin/gateway_postrun_seekrit.py)) reads your secret store and gives it the values the rules name.

When changing it, keep these properties:

* Every host without a rule refused, and `tls` hosts passed through, not read.
* Secrets only over TLS, only to the exact hosts and methods of the rules naming them.
* The secrets only ever in its memory: never in its configuration, environment, files or logs, nor on a command line.
* The gateway unprivileged, without capabilities or a shell, and its build pinned by full commit hash, with every patch applied cleanly.
* `network-gateway-reads-tls: 'yes'` for it, and the resources directory inside one only you can use.

Its own weak points are on the [seekrit gateway attack vectors](network-gateway-seekrit-attack-vectors.md) page.

### Gateway: Envoy

[`containers/network-gateway-envoy`](../../containers/network-gateway-envoy/) runs [Envoy](https://www.envoyproxy.io)'s distroless image, pinned by digest: no shell, no package manager.  It runs as an unprivileged user (65532), with no capabilities, two worker threads, and its access log as JSON on standard output (`scoter netlog` reads it).  It provides N2, and backs up N3.  It passes `tls` rules through, on any port in `network-gateway-ports`, each rule on its own port, and reads `https` rules for exact hosts, on the ports in `network-gateway-tls-ports`, to check their methods, and fills in the secrets those rules name, in request header values, with Surf Scoter's secrets filter (a Wasm module built into its image).  The secrets reach it after it starts, from `network-gateway-post-start` (`gateway_postrun_envoy.py`), into its memory only; see [ENVOY-10](network-gateway-envoy-attack-vectors.md#envoy-10-secrets-in-the-gateways-memory-and-their-delivery).

For `tls` rules it uses Envoy's [dynamic forward proxy](https://www.envoyproxy.io/docs/envoy/v1.36.12/configuration/http/http_filters/dynamic_forward_proxy_filter), which resolves the requested host and connects to it.  Envoy's documentation warns that such a proxy is open to confused deputy attacks.  [`gateway_envoy.py`](../../py/src/dist_bin/gateway_envoy.py) writes its configuration (`/user-space/gateway/envoy.json`) so that:

1. Only `CONNECT` is routed, over HTTP/1 only.
2. Each rule is a route matching the request's whole `:authority`, port included: `host:port` exactly, or, for `*.domain`, a regular expression for its subdomains, at any depth, on that port (not the domain itself).  Case is ignored.  Anything else matches no route, and gets a 404.
3. The host the route matched is the host that's resolved and dialled: nothing rewrites it, and nothing can hand the proxy another one (no host rewrites, no host from filter state, no port stripping, no other filters).
4. A host is only resolved for a route that matched, so an unlisted name never reaches DNS.
5. A `tls` tunnel is plain TCP: the proxy's cluster has no TLS of its own.
6. A read host's `CONNECT` goes to an internal listener for that host and port alone, never through the dynamic forward proxy.  It ends the TLS connection with a certificate for that host, made for the run by `network-gateway-mk-leaf` and signed by the run's CA, and sends on only the requests whose `:authority` is that host, with the rules' methods; anything else gets a 403.  It sends them to a cluster that names the host, fixed, over TLS that checks the server's certificate against the system's CAs and the host's name.
7. There's no admin interface, and only IPv4 addresses are looked up.

When changing it, keep these properties, the [tests](../../py/tests/test_gateway_envoy.py) that check them, and:

* Envoy stays unprivileged and without capabilities, its image pinned by digest.
* Its `network-gateway-reads-tls` stays `'no'`: it's never given the run's CA key, only a certificate for each host it reads.  Those keys are in its configuration, which is readable by others, so the resources directory stays inside one only you can use.
* A read host's HTTP/2 windows stay small, so a slow reader can't fill the gateway's memory.

Its own weak points are on the [Envoy gateway attack vectors](network-gateway-envoy-attack-vectors.md) page.


## What this doesn't protect

**Settings are trusted completely.**  Nothing checks that a replacement compose file, firewall or gateway keeps the required properties.  If you change them, test them; see the checklist below.

**Default images aren't pinned.**  `alpine:3` and `debian:bookworm-slim` are tags, not digests, so a rebuild can pick up a different base.

**Separate environments need verification (N7).**  The default `compose-up` command doesn't pass a compose project name, so compose derives one from the resources directory's name (`resources`) for every project.  Two environments running at once may share, or replace, each other's containers and networks.

**Restarts need verification (N6).**  The firewall and gateway use `restart: unless-stopped`.  Confirm that if the firewall container restarts, the gateway can't keep carrying traffic without the rules in place.

**The gateway is a large attack surface.**  If it's subverted, N4 still limits it to TCP 443 on public addresses, but not to the allowed hosts.  A subverted seekrit gateway also holds the plaintext of the hosts it reads, your secrets and the run's CA key.

**The seekrit gateway's secrets live in its memory.**  After a gateway restart, they're gone until `scoter gateway-post-start`; anyone who can `docker exec` into it can give it values of their own first.

**The image's default list allows nothing.**  The gateway image has an empty `/user-space/egress-hosts.txt`, so its configuration parses; `scoter run` mounts the list it made over it.


## Checklist after a change

Run these against a running environment whenever you change any of these files.  The [containers README](../../containers/README.md) has more detail.

1. From the dev container, an allowed domain works: `curl -sI https://github.com`.
2. An unlisted domain is refused with a 403: `curl -sI https://example.com`.  `scoter netlog` shows it, refused.
3. A direct connection fails: `curl -sI --noproxy '*' --connect-timeout 3 https://github.com`.
4. External DNS fails: `getent hosts example.com`.
5. The firewall table is loaded, and its drop counters move: `podman exec HOSTNAME-fr nft -a list table inet egress`.
6. No container publishes a port: `podman port HOSTNAME`, and the same for `HOSTNAME-fr` and `HOSTNAME-ng`.
7. Two environments started at once each get their own isolated network: `podman network ls`.

With the seekrit gateway, also:

8. A `tls` host shows its own certificate, not the run's CA: `curl -sv https://pypi.org/ 2>&1 | grep issuer`.
9. A secret goes only where its rule says, and the container holds only the placeholder: `printenv NAME` shows `{{seekrit:NAME}}`, and `scoter netlog` shows the request `with NAME`.
10. A method the rule doesn't give is refused with a 403, and a placeholder in a plain `http://` request too.
11. No value shows in `podman inspect HOSTNAME-ng`, `podman logs HOSTNAME-ng`, or the gateway's configuration.

[`07-seekrit-gateway`](../../integration-tests/07-seekrit-gateway/run.sh) checks these.
