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
