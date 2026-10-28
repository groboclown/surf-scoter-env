# User and Default Settings

Much of what keeps an environment contained comes not from the project or from Surf Scoter's code, but from files Surf Scoter ships as defaults:

* the default system configuration, [`defaults/setup.yaml`](../../defaults/setup.yaml);
* the compose file that wires the containers together, [`defaults/docker-compose.yaml`](../../defaults/docker-compose.yaml);
* the firewall container, [`containers/firewall-router`](../../containers/firewall-router/);
* the gateway container, [`containers/network-gateway`](../../containers/network-gateway/).

You can replace every one of them through your own [system configuration](../system-config-file.md).

**How to weigh these.**  Surf Scoter maintains and reviews the shipped defaults, but changes you make are yours to review.  These files come from you, not from a project you cloned, so they rank below project-supplied files as a target for analysis.  They still deserve their own scrutiny: together they provide almost every [required network property](network.md#required-properties).  A mistake in a project file affects that project.  A mistake here silently removes the protection for every project you run.


## Where settings come from

The [system configuration file](../system-config-file.md) describes the loading rules in full.  The parts that matter for security:

* Settings load from the installation's `setup.yaml` (lowest priority), then your own setup file (highest).  A higher priority file replaces individual keys.
* The tools **never** read setup files from a project.  `$SURF_SCOTER_HOME`, your setup file, and a `distribution-dir` taken from a setup file must all lie outside the project, its clone, and the repository holding it; otherwise the tools stop without making a configuration.  This matters because a project can set `SURF_SCOTER_HOME` itself, through tools such as direnv that run when you enter its directory.
* `scoter` reports when your setup weakens one of the *guarded* settings, such as removing a default check or lengthening the exemption limit, and `scoter run` refuses to start when a setup program resolves into the project or through a relative path.  See [guarded settings](../system-config-file.md#guarded-settings).
* A setup file can run any program on your computer: every `runs` entry is a command line, and every `programs` entry picks an executable.  It also decides what a project may do, such as the project files directory and where dev containers may come from.  Treat a setup file like a shell script.


## The system configuration

These keys from [`defaults/setup.yaml`](../../defaults/setup.yaml) carry the most security weight.

| Key | What it does | What to keep in mind when changing it |
| --- | --- | --- |
| `docker: programs: exec` and `docker: runs: *` | Build images, start and stop the environment, start the bridge broker | These run on your host with your privileges.  `broker` must not request a TTY, and must run inside the named container. |
| `ssh: runs: proxy-command` | How SSH reaches the container | Keep it going through the runtime's exec channel.  Switching to a published port breaks N5. |
| `ssh: paths: key-file`, `known-hosts`, `config-file` | Your client key and the files `scoter` edits | `scoter` rewrites the config and `known_hosts` files on each run. |
| `project-state: paths: *` | Where the project clone, setup and resources directories live | Keep all three, and the directories above them, where the container can't write: the clone is mounted read-write, so nothing may be inside it, and a directory the container could replace with a symlink would redirect where `scoter` clones and deletes ([FILE-10](files-attack-vectors.md#file-10-the-clones-path-through-container-symlinks)).  The default keeps them outside the project, in `state-dir` (FILE-9). |
| `project-state: values: persistent-store-volume`, `persistent-store-prefix` | The dev container's persistent store, and how `scoter cleanup` recognizes the tools' volumes | Keep the name different for each project, or two projects' containers share one store.  A prefix that matches other volumes lets `scoter cleanup --persistent-stores` remove them. |
| `git-vcs: runs: *`, `file-vcs: runs: *` | How the project clone is created, refreshed and merged back | `fetch-changes` runs on your host with the clone as its working directory (see [file access attack vectors](files-attack-vectors.md#file-1-scoter-run-runs-git-inside-the-project-clone)). |
| `docker: runs: create-volume`, `remove-volume`, `list-volumes` | The persistent store volume | `create-volume` must succeed for a volume that's already there, or every run after the first fails. |
| `dev-env: runs: network-gateway-write-config` | Turns the egress rules into the gateway's configuration | A command that writes a broader list than the rules say breaks N2. |
| `dev-env: values: network-gateway-name` | The gateway the egress rules are accepted for | Changing it asks for a new review; a gateway that enforces less than its name suggests weakens N2. |
| `dev-env: paths: docker-compose`, `firewall-router`, `network-gateway` | Which compose file and network containers to use | Replacing these replaces the network.  Recheck every property. |
| `dev-env: values: *-image` | Image names for the containers | An image from a registry rather than a local build means trusting whoever publishes it. |
| `dev-env: values: mem-limit`, `cpus`, `pids-limit`, `logging-max-size` | Dev container resource limits | Removing them lets the container exhaust your host. |
| `verification: host-commands`, `project-files`, `egress-hosts`, `value-lists: egress-refused-wildcards`, `path-lists: public-suffix-lists` | The checks the review runs on a project's settings | Weakening them weakens every later review.  Only the `host-commands` checks are recorded with an acceptance; a change to the others doesn't ask for a new review of projects already accepted ([VER-6](verification-attack-vectors.md#ver-6-setup-changes-that-dont-ask-for-a-new-review)). |
| `verification: paths: store` | Where your acceptances are kept | Keep it outside every project and its clone, private to you, and never mounted into a container ([VER-8](verification-attack-vectors.md#ver-8-the-store-itself)). |
| `verification: values: expiry-days`, `exempt-days`, `max-exempt-days` | How long an acceptance and an exemption last | Longer values let an acceptance outlive the reasons it was given ([VER-4](verification-attack-vectors.md#ver-4-exemptions-accept-what-nobody-reviewed)). |
| `verification: values: max-file-size`, `max-snapshot-size` | Limits on what a snapshot keeps | A project over the limits can't be accepted; raising them costs disk space, not safety. |
| `scoter: path-lists: dev-containers-search`, `verification: path-lists: verified-containers` | Where containers are found by name, and which of those are reviewed | A container found here outside the project is trusted, not reviewed.  Never add a directory a project controls ([VER-9](verification-attack-vectors.md#ver-9-trusted-containers)). |


## The compose file

[`defaults/docker-compose.yaml`](../../defaults/docker-compose.yaml) is where the network properties are actually wired up.  `scoter run` copies it into the environment's resources directory and fills it in from a generated `.env` file.  Some of those values come from the project's `dev-env.yaml`; see [dev container attack vectors](dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file).

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

[`containers/network-gateway`](../../containers/network-gateway/) runs Squid on Debian as the unprivileged `proxy` user, with no capabilities, caching off, and the access log on standard output (`podman logs HOSTNAME-ng`).  It provides N2, and backs up N3.

[`squid.conf`](../../containers/network-gateway/squid.conf) applies its rules in this order:

1. Refuse anything that isn't `CONNECT`.
2. Refuse any port but 443.
3. Refuse any hostname not on the allowed host list (`/user-space/egress-hosts.txt`, made from the egress rules and mounted read-only).  Each line is a regular expression (`dstdom_regex`): `^host$` for a host, `^.+\.domain$` for `*.domain` (its subdomains, not the domain itself).  `-i` ignores case, and `-n` stops Squid from making reverse DNS lookups to test the list.
4. Refuse destinations that resolve into the [blocked destination list](../../containers/network-gateway/blocked-destinations.txt): private, loopback, link-local, carrier-grade NAT, multicast, documentation and reserved ranges, for IPv4 and IPv6.
5. Allow what's left.

The order matters.  The hostname check comes before the address check, so an unlisted name is refused before Squid resolves it, and a request can't make Squid send a DNS query for an attacker's domain.

When changing it, keep these properties:

* The rule order stays as it is, and there's no `http_access allow all`.
* Only port 443 is allowed.
* Squid stays unprivileged and without capabilities.
* The blocked destination list stays in the image, out of reach of the user and the project.
* There's no TLS interception, unless you've thought through what it means.  It needs a private certificate authority trusted inside the container, and it puts every request's contents in Squid's hands.


## What this doesn't protect

**Settings are trusted completely.**  Nothing checks that a replacement compose file, firewall or gateway keeps the required properties.  If you change them, test them; see the checklist below.

**Default images aren't pinned.**  `alpine:3` and `debian:bookworm-slim` are tags, not digests, so a rebuild can pick up a different base.

**Separate environments need verification (N7).**  The default `compose-up` command doesn't pass a compose project name, so compose derives one from the resources directory's name (`resources`) for every project.  Two environments running at once may share, or replace, each other's containers and networks.

**Restarts need verification (N6).**  The firewall and gateway use `restart: unless-stopped`.  Confirm that if the firewall container restarts, the gateway can't keep carrying traffic without the rules in place.

**Squid is a large attack surface.**  If it's subverted, N4 still limits it to TCP 443 on public addresses, but not to the allowed domains.

**The image's default list allows nothing.**  The gateway image has an empty `/user-space/egress-hosts.txt`, so its configuration parses; `scoter run` mounts the list it made over it.


## Checklist after a change

Run these against a running environment whenever you change any of these files.  The [containers README](../../containers/README.md) has more detail.

1. From the dev container, an allowed domain works: `curl -sI https://github.com`.
2. An unlisted domain is refused with a 403: `curl -sI https://example.com`.
3. A direct connection fails: `curl -sI --noproxy '*' --connect-timeout 3 https://github.com`.
4. External DNS fails: `getent hosts example.com`.
5. The firewall table is loaded, and its drop counters move: `podman exec HOSTNAME-fr nft -a list table inet egress`.
6. No container publishes a port: `podman port HOSTNAME`, and the same for `HOSTNAME-fr` and `HOSTNAME-ng`.
7. Two environments started at once each get their own isolated network: `podman network ls`.
