# Contributors' Guide

*For further details, see the [handbook](docs/contributors/README.md).*

## Requirements

You'll need these things installed to get building:

* A Python 3.12 or better interpreter.
* Rust's `cargo` tool.
* `make` 
* Podman or Docker.

## Running From This Directory

So you've cloned down the `surf-scoter-env` project, and want to kick off a local test so you can contribute back to this project.  Thanks!

For a quick up-and-running experience, try:

```shell
make dev-prep
```

This will create the images and executables, and put them in the right places.

## Working on Surf Scoter With Surf Scoter

You can develop Surf Scoter inside a Surf Scoter environment, but don't run the tools from the checkout you're working on.  When Surf Scoter is the project, whatever the container writes becomes, once you merge it, code that runs on your computer the next time the tools run from that checkout: the scripts, `setup.yaml`, the container definitions, and the Rust source (`bin/gen-scoter-uc` from `make dev-prep` builds and runs it with `cargo run`).  A mistake in review would then hand the container your computer.

Instead, install a separate copy of the tools from a commit you've reviewed, and run that copy against the checkout:

```shell
make self-host-install
python3 ~/.local/share/surf-scoter-self-host/scoter.py run
```

`make self-host-install`:

* exports `SELF_HOST_REF` (default `main`) with `git archive`, so only committed files go in: never uncommitted changes, untracked files, or the workspace;
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

* ***NO OPEN SECURITY ISSUES.*** Because of the sensitivity of this project, releases must not have open security issues.
* **All builds pass.** All builds pass.  Includes lint and tests.
