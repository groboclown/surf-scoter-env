# Grab Bag of Future Ideas

## Full Network Intercept

Rather than using a squid proxy, this could use something like [mitmproxy](https://docs.mitmproxy.org/stable/) for full inspection and interactive proxying.  It adds complexity requiring sharing a root CA into the development environment.

Currently, without the HTTPS inspection, a container can allow `npm` and `pypi` and `github` uploads (and whatever else is in the allow list domains) - thus, they can become exfiltration channels.  Until the switch-over, this will remain an issue.


## The gateway's other protocols and ports

The Squid gateway enforces only `tls` on 443.  `http` (with `methods`) needs port 80 in the firewall's rules and Squid's HTTP handling; other ports need firewall changes (N4); `protocol: https`, and `methods` on it, need a gateway that looks inside TLS.  Each is refused until then.  A test that runs the gateway containers belongs with this work too (step 18's batch C has the first one).


## Test on Windows

Left for a pass with a Windows machine to test on:

* Matching `name-patterns` against the real file's name drops its extension, whatever it is, rather than only a `PATHEXT` one.
* The verified store's owner and mode checks, and the path checks' owner and mode checks, are skipped; the store design's "under the user's profile" check isn't written.
* Case-insensitive file systems: the environment id doesn't fold the path's case.


## Pulling without a person

For automation.  `scoter pull` needs a terminal, and has no `--yes`: approval is meant to follow a person's look.  A setup can already name a reviewer of its own (`pull: runs: reviewer`), which approves by whatever rule it likes, so automation isn't prevented, only discouraged ([VER-27](user-owned-project-files.md#ver-27-approval-cant-prove-a-person-reviewed)).  A stock policy reviewer could make the safe version easy: approve only when no finding warns or refuses, nothing changed since the base but files a list names, and every host command's program is one the user already approved; otherwise deny, saying why, for a person to `scoter pull` by hand.  It would build on `scoter pull analyze`'s report and its finding ids, like any reviewer.


## A `drop-env` for Docker

A recent change added `value-lists: drop-env` and gave `git-vcs` one (`GIT_*`), but not `docker`.  A project's `.envrc` could set `DOCKER_HOST`, `DOCKER_CONTEXT` or `DOCKER_CONFIG` (whose credential helpers run programs) for the tools' Docker commands.  Dropping them by default would break Podman and remote-daemon setups, which set `DOCKER_HOST` themselves; [the system configuration file](../system-config-file.md#value-lists) suggests one for setups that don't.  Consider a default that keeps working for them: say, the `docker` section's `runs` passing the daemon explicitly (`--host ${docker:host}`, from a `values: host` the user sets once), so `DOCKER_HOST` can be dropped.


## Matching `env` patterns against patterns

`env` entries match a command's `allow` entries as exact text.  Matching patterns against patterns (does `allow: [GIT_*]` pass `GIT_SSH_COMMAND`?) is future work.


## Allow `scoter user-setup` to work with automation

The current defaults with `--non-interactive` use the safest approach.  Automation tooling should have an option to perform the "needs to exist but requires modifications" actions to run.  Similar to the `--force` versus `--dry-run` with the cleanup sub-command.


## Allow `scoter user-setup` to download and update the public-suffix-lists

Because the tool shouldn't ship with the public suffix list, instead it should give the end-user the opportunity to download it and update the public-suffix-lists to point to that local download.


## Replace consumed Dockerfile parent tag

When importing a `Dockerfile` and `dev-env.yaml` into the locked stage, the user declares the parent image tags.  However, this leads to [security issues](../security/dev-container-attack-vectors.md##dev-15-the-project-chooses-shared-image-tags).

Instead, that tag represents *what the Dockerfile references*, not the actual tag.  The import would use the user configuration prefix + a generated suffix and replace that in the Dockerfile.

The import analysis would need to flag all Dockerfile `FROM` tags that do not have a listed parent.  These represent external parent images not directly under the user's review.


## Reshuffle Surf Scoter Files

The files in this repository grew organically as the design and ideas grew.  It now seems to make more sense to organize this as a monorepo.  That will make out-of-the-box usage of Surf Scoter harder - it means the build-scripts do more work.  However, it puts related things next to related things.  It will also make sub-project handling nicer for IDEs.

- Python scripts kept together.  This allows for a `pyproject.toml` file and better build tool support.
- All the rust programs together.  This will need 2 libraries (the host-bridge communication + the SUC format) and 4 executables.  Probably put them as a directory with each as separate Rust projects.
- Installer as its own thing.  This will contain the defaults.  Maybe also the templates.
- Containers in their own directory, like now.  To support the host-bridge executables, it'll need to have a local version (requires a copy into the directory) and one that pulls from GitHub.
- Use proper `Makefile` splitting per directory.


## Network Gateway Follow-ups

Ideas left over from adding the seekrit gateway (see the [seekrit gateway's attack vectors](../security/network-gateway-seekrit-attack-vectors.md)):

- **Port 80, and a test server.**  Opening TCP 80 in the firewall ([`egress.nft`](../../containers/firewall-router/egress.nft)), with `dev-env: value-lists: network-gateway-ports`, would let `http` rules work with the seekrit gateway; secrets still never go over it.  With an allowed test-server CIDR (seekrit's `[egress] allow_cidr`), integration tests could run their own server to check what the gateway lets through, refuses, and sends.
- **Wildcard hosts with the seekrit gateway.**  seekrit-proxy matches hosts exactly, so the gateway refuses `*.` rules.  A patch for suffix matching, with the same public-suffix refusals as the generator, would let it take them.
- **Name constraints on the run's CA.**  The dev container trusts the run's CA for every host.  Limiting it to the hosts with `https` rules (X.509 name constraints) would stop a stolen key from intercepting anything else.
- **Per-gateway hardening in the compose file.**  `read_only: true` and a fixed `user:` suit the seekrit gateway, but not Squid, which writes at run time; the compose file has one gateway service for both.
- **Paths in egress rules.**  seekrit-proxy can limit a rule to paths as well as methods, which would narrow what a secret can be used for.
- **Upstream the patches.**  The ports (0001) and CA loading (0006) patches stand alone, and fix upstream behavior; the fewer patches, the less rebasing.
- **An OpenTelemetry collector.**  seekrit-proxy can export traces and metrics; a collector beside the gateway would give `scoter netlog` history beyond the container's log.
- **A corporate TLS-inspection CA.**  On a network that re-signs TLS, the seekrit gateway can't verify the hosts it reads (it trusts only its built-in roots).  An `upstream_ca` setting, from the user's setup, would add theirs.
