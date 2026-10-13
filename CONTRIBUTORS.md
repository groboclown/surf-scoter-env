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
