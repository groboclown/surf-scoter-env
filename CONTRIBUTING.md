# Contributors' Guide

*For further details, see the [handbook](docs/contributors/README.md).*

## Requirements

You'll need these things installed to get building:

* A Python 3.12 or better interpreter.
* Rust's `cargo` tool.
* `make` 
* Podman or Docker.

To generate the SBOM and scan those, you'll need [`cdxgen`](https://github.com/cdxgen/cdxgen#installing) and [`osv-scanner`](https://google.github.io/osv-scanner/installation/):

* Don't have them but have Go installed?  Run `make dev-install` to put them on your computer.  `cdxgen` (a standalone binary, Linux or macOS) will go into `target/sbom-gen`, `osv-scanner` will go into your `$HOME/go/bin` directory.
* Have them installed?  Run:
    * `make CDXGEN=$( where cdxgen ) OSVSCANNER=$( where osv-scanner ) sbom scan`


## Repository layout

| Directory | What's there | Installed as |
|---|---|---|
| [`py/`](py/) | The Python sources, with the developer tools' settings in [`pyproject.toml`](py/pyproject.toml). | |
| [`py/src/dist_root/`](py/src/dist_root/) | `scoter.py`, the front end. | `scoter` at the distribution's top |
| [`py/src/dist_bin/`](py/src/dist_bin/) | The sub-command and helper scripts, and their library, `surf_scoter_lib`. | the distribution's `bin/` |
| [`py/tests/`](py/tests/) | Their unit tests: `make script-tests`. | |
| [`rust/host/`](rust/host/) | A Cargo workspace: the configuration library ([`config`](rust/host/config/)) and the programs that use it, [`scoter-host`](rust/host/scoter-host/) and [`gen-scoter-uc`](rust/host/gen-scoter-uc/). | the distribution's `bin/` |
| [`rust/container/`](rust/container/) | A Cargo workspace that stands on its own: the wire protocol library ([`comm`](rust/container/comm/), which `rust/host` uses too) and the dev container's bridge programs, [`client`](rust/container/client/) and [`broker`](rust/container/broker/). | built inside the dev-env image |
| [`dist-files/`](dist-files/) | Files installed as they are, laid out as in the distribution: `setup.yaml`, `defaults/`, `templates/`. | the distribution's top |
| [`containers/`](containers/) | The images: the dev container's base, the firewall and the network gateways. | `containers/` |
| [`build-scripts/`](build-scripts/) | Installer and developer scripts. | |
| [`integration-tests/`](integration-tests/) | End-to-end tests, run by hand. | |
| [`docs/`](docs/) | The documentation. | `docs/` |

### The dev-env image and `rust/container`

The distribution doesn't ship the bridge programs' source.  [`containers/dev-env/Dockerfile`](containers/dev-env/Dockerfile) fetches just the `rust/container` directory of this repository with a sparse `git clone`, at the commit its `SCOTER_COMMIT` argument names by its full id, and checks it got that commit.  So `rust/container` must never use anything outside itself.

For working on Surf Scoter, `make dev-env-image` instead copies your checkout's `rust/container` into a staged build context ([`stage-dev-env.sh`](build-scripts/stage-dev-env.sh)) and builds with `--build-arg SCOTER_SOURCE=local`.

When a change to `rust/container` is merged, set `SCOTER_COMMIT` to that commit, in a commit of its own.  `make dev-env-pin-check` (part of `make lint`) warns when `rust/container` has changed since the pinned commit.


## Working on Surf Scoter With Surf Scoter

So you've cloned down the `surf-scoter-env` project, and want to kick off a local test so you can contribute back to this project.  Thanks!

You have two approaches, depending on how confident you are on using Surf Scoter:

### Trusting

You trust Surf Scoter to be built by capable hands without malicious intent, and that all downloaded dependencies don't try to install malware.  That's common practice.

You can run:

```shell
./build-scripts/linked-install.sh ~/.local/share/surf-scoter-linked
```

This will create links from the `~/.local/share/surf-scoter-linked` directory into the Surf Scoter project, so that, as you change code, the directory receives updates.

You can then run scoter with:

```shell
~/.local/share/surf-scoter-linked/scoter help
```

Or, more helpfully:

```shell
alias scoter='~/.local/share/surf-scoter-linked/scoter '
scoter help
```

If the number of files in the [`py/src/dist_bin`](py/src/dist_bin/) directory changes, you'll need to rerun `linked-install.sh` to adjust the file links.

### It's Risky

You're making changes that you don't trust inside the container, and so don't want those changes affecting how the dev environment gets built.  Smart.  When Surf Scoter is the project, whatever the container writes becomes, once you merge it, code that runs on your computer the next time the tools run from that checkout: the scripts, `setup.yaml`, the container definitions, and the Rust source (`bin/gen-scoter-uc` from `make dev-prep` builds and runs it with `cargo run`).  A mistake in review would then hand the container your computer.

Install a separate copy of the tools from a commit you've reviewed, and run that copy against the checkout:

```shell
make self-host-install
python3 ~/.local/share/surf-scoter-self-host/scoter.py run
```

`make self-host-install`:

* exports `SELF_HOST_REF` (default `main`) with `git archive`, so only committed files go in: never uncommitted changes, untracked files, or the project clone;
* builds the host programs from that export (not from your checkout);
* installs the result in `SELF_HOST_DIR` (default `~/.local/share/surf-scoter-self-host`), outside the repository, read-only, with a `.surf-scoter-self-host` file recording the commit.

For example, to try the tools from a branch you've reviewed:

```shell
make self-host-install SELF_HOST_REF=my-reviewed-branch
```

Run it again to update the copy; it only ever replaces a directory it installed.  `make self-host-remove` removes it.

The copy finds itself as the distribution directory, so it doesn't need `--distribution-dir`, and it never reads this checkout's `setup.yaml`, scripts or containers.  The checkout is only the project: its `.surf-scoter.d` and its source.

Don't point a `scoter: paths: distribution-dir` setting at the checkout either: the tools refuse a `distribution-dir` from the setup files that's inside the project's repository.


## Releasing

New releases must pass these gates:

* Update dependencies
    * Python [`dev-requirements.txt`](py/dev-requirements.txt)
    * Rust cargo files, under [rust](rust/) and [network-gateway-envoy](containers/network-gateway-envoy/secrets/).
    * [seekrit-dev `SEEKRIT_COMMIT`](containers/network-gateway-seekrit/Dockerfile) + [patches](containers/network-gateway-seekrit/patches/)
    * Container base images.
        * [dev-env](containers/dev-env/Dockerfile)
        * [firewall-router](containers/firewall-router/Dockerfile)
        * [network-gateway-envoy](containers/network-gateway-envoy/Dockerfile)
        * [network-gateway-seekrit](containers/network-gateway-seekrit/Dockerfile)
        * [network-gateway-squid](containers/network-gateway-squid/Dockerfile)
    * [cdxgen version](Makefile)
* Major and minor versions:
    * ***NO OPEN SECURITY ISSUES.*** Because of the sensitivity of this project, releases must not have open security issues.
    * Review [`SECURITY.md`](SECURITY.md) for supported versions.
* Patch versions:
    * Open security issues are fine, if the patch covers at least one security issue.  This allows incremental fixes for security issues as they close, rather than waiting for all fixes to come in.
* Bump version numbers:
    * [host + generator](rust/host/Cargo.toml) (`workspace.package`)
    * [client + bridge](rust/container/Cargo.toml) (`workspace.package`)
    * [comm library](rust/container/comm/Cargo.toml) (`package`)
    * [Python sources](py/pyproject.toml)
    * [`version.txt`](version.txt)
    * TODO scoter.py when it supports the version argument.
* `SCOTER_COMMIT` in [`containers/dev-env/Dockerfile`](containers/dev-env/Dockerfile) names a commit with the release's `rust/container`, and `make dev-env-pin-check` is quiet.
* Changelog updated.
* Changes merged into the `main` branch.
* **All builds pass.** All builds pass.  Includes lint and tests.
