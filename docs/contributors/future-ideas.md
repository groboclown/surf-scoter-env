# Grab Bag of Future Ideas

## Full Network Intercept

Rather than using a squid proxy, this could use something like [mitmproxy](https://docs.mitmproxy.org/stable/) for full inspection and interactive proxying.  It adds complexity requiring sharing a root CA into the development environment.

Currently, without the HTTPS inspection, a container can allow `npm` and `pypi` and `github` uploads (and whatever else is in the allow list domains) - thus, they can become exfiltration channels.  Until the switch-over, this will remain an issue.


## Choosing the setup for a project run

`--user-setup` was removed: with the egress rules in the project file, and the accepted settings in the user's own storage, a project no longer needs its own setup file.  The remaining need is to run a project with a different docker-compose file and related images (say, an alternate gateway, or extra service containers).

Instead of reading another setup file, let the user choose, for a project run, which setup section to use for those parts, from sections their own setup files (or the distribution's) define.  The choice is the user's, recorded with the project's accepted settings, never the project's.

This also bears on the accepted egress rules: they were reviewed for one gateway (step 9), so a section with a different gateway, or a different compose file, may enforce them differently.  Switching sections should check the accepted rules against what the chosen section's gateway can enforce, and ask for a new review when it can't enforce them, or enforces them differently.  This also applies to the host command blocking rules.

## The gateway's other protocols and ports

From steps 7 and 9: the Squid gateway enforces only `tls` on 443.  `http` (with `methods`) needs port 80 in the firewall's rules and Squid's HTTP handling; other ports need firewall changes (N4); `protocol: https`, and `methods` on it, need a gateway that looks inside TLS.  Each is refused until then.  A test that runs the gateway containers belongs with this work too (step 18's batch C has the first one).

## Windows

Left from steps 4, 5, 11 and 15, for a pass with a Windows machine to test on:

* Matching `name-patterns` against the real file's name drops its extension, whatever it is, rather than only a `PATHEXT` one.
* The verified store's owner and mode checks, and the path checks' owner and mode checks, are skipped; the store design's "under the user's profile" check isn't written.
* Case-insensitive file systems: the environment id doesn't fold the path's case.
* The review prompt's `read_once` has no `O_NOFOLLOW`, and skips the owner check.

## Accepting a review file without the prompt (`--accept-file`)

For automation, out of scope for now (moved from step 18; step 12's design names it).  The draft:

`scoter verify --accept-file FILE`, for automation: no terminal and no prompt.  The file, a review the user wrote earlier (`scoter verify --output`, then edited), is read once as the prompt reads an edit (no link, a regular file of the user's), parsed and checked the same way (the generator's `--review`, then `checks.check_review`), and stored if it can be used; otherwise the command fails, listing why.  It never accepts a project's suggestion by itself: only a file the user gives.

The review has to be for the files as they are now.  The prompt has that by construction (it snapshots the files, then writes the review from them); a file written earlier doesn't.  So the format gains a `snapshot` key: the snapshot id the review was made from, written by every review, read-only like `project-dir`.  `--accept-file` refuses a file whose `snapshot` isn't the current one ("the project's files changed since this review was written"), or that has none.  The prompt checks it too, for an edit that changed it.  An earlier acceptance's `review.yaml` (`--previous-review`) may lack it.

## A `drop-env` for Docker

Step 18 (batch B) added `value-lists: drop-env` and gave `git-vcs` one (`GIT_*`), but not `docker`.  A project's `.envrc` could set `DOCKER_HOST`, `DOCKER_CONTEXT` or `DOCKER_CONFIG` (whose credential helpers run programs) for the tools' Docker commands.  Dropping them by default would break Podman and remote-daemon setups, which set `DOCKER_HOST` themselves; [the system configuration file](../system-config-file.md#value-lists) suggests one for setups that don't.  Consider a default that keeps working for them: say, the `docker` section's `runs` passing the daemon explicitly (`--host ${docker:host}`, from a `values: host` the user sets once), so `DOCKER_HOST` can be dropped.

## Matching `env` patterns against patterns

From step 4: `env` entries match a command's `allow` entries as exact text.  Matching patterns against patterns (does `allow: [GIT_*]` pass `GIT_SSH_COMMAND`?) is future work.

## Allow `scoter user-setup` to work with automation

The current defaults with `--non-interactive` use the safest approach.  Automation tooling should have an option to perform the "needs to exist but requires modifications" actions to run.  Similar to the `--force` versus `--dry-run` with the cleanup sub-command.

## Allow `scoter user-setup` to download and update the public-suffix-lists

Because the tool shouldn't ship with the public suffix list, instead it should give the end-user the opportunity to download it and updat ethe public-suffix-lists to point to that local download.
