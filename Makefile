# Simple makefile to have a unified place to see standard
# developer tasks.

CARGO ?= cargo
ZIP ?= zip
DOCKER ?= docker
CARGO_BIN ?= ~/.cargo/bin
PYTHON ?= python3

LOCAL_CONTAINER_TAG_PREFIX ?= local-surf-scoter-env/
DEV_ENV_TAG = $(LOCAL_CONTAINER_TAG_PREFIX)dev-env:build
FIREWALL_ROUTER_TAG = $(LOCAL_CONTAINER_TAG_PREFIX)firewall-router:build
NETWORK_GATEWAY_TAG = $(LOCAL_CONTAINER_TAG_PREFIX)network-gateway:build


# ---------------------------------------------------------------------
# Top-level tasks.

## help                      This screen.
# To use help, add a '##' line above the target.
# Description starts at column 30 (in this file).
.PHONY: help
help:
	@grep -E '^##' $(MAKEFILE_LIST) | cut -c 4-


## clean                     Remove build-created files.
.PHONY: clean
clean:


## build                     Construct all tool-generated items.
.PHONY: build
build:


## test                      Run all tests.
.PHONY: test
test:


## fmt                       Alter files to use the project-standard file formatting rules.
.PHONY: fmt
fmt:


## lint                      Check the files for conformance issues.
.PHONY: lint
lint:


## dev-install               Install development dependencies (non-OS distribution files)
.PHONY: dev-install
dev-install:

# ---------------------------------------------------------------------
# Running Surf Scoter on itself.  See CONTRIBUTORS.md.

SELF_HOST_REF ?= main
SELF_HOST_DIR ?= $(HOME)/.local/share/surf-scoter-self-host

## self-host-install         Install a copy of the tools from SELF_HOST_REF (default: main) into
##                           SELF_HOST_DIR, to run against this checkout.
.PHONY: self-host-install
self-host-install:
	build-scripts/self-host-install.sh install "$(SELF_HOST_REF)" "$(SELF_HOST_DIR)"

## self-host-remove          Remove the copy installed by self-host-install.
.PHONY: self-host-remove
self-host-remove:
	build-scripts/self-host-install.sh remove "$(SELF_HOST_DIR)"

# ---------------------------------------------------------------------
# Rust-related tasks.

## dev-env-rust-tests        Run unit tests for the `dev-env` container tools.
.PHONY: dev-env-rust-tests
test: dev-env-rust-tests
dev-env-rust-tests:
	cd containers/dev-env && $(CARGO) test

## dev-env-rust-clean        Clean the generated rust files from the 'dev-env' container.
.PHONY: dev-env-rust-clean
clean: dev-env-rust-clean
dev-env-rust-clean:
	-rm -rf containers/dev-env/target

## dev-env-rust-fmt          Run 'fmt' on the 'dev-env' container source files.
.PHONY: dev-env-rust-fmt
fmt: dev-env-rust-fmt
dev-env-rust-fmt:
	cd containers/dev-env && $(CARGO) fmt --all

## dev-env-rust-lint        Run 'clippy' on the 'dev-env' container source files.
.PHONY: dev-env-rust-lint
lint: dev-env-rust-lint
dev-env-rust-lint:
	cd containers/dev-env && $(CARGO) clippy --no-deps --locked


## host-rust-tests           Run unit tests for the 'host' program.
.PHONY: host-rust-tests
test: host-rust-tests
host-rust-tests:
	cd host && $(CARGO) test

## host-rust-clean           Clean the generated rust files from 'host'.
.PHONY: host-rust-clean
clean: host-rust-clean
host-rust-clean:
	-rm -rf host/target

## host-rust-fmt             Run 'fmt' on the 'host' source files.
.PHONY: host-rust-fmt
fmt: host-rust-fmt
host-rust-fmt:
	cd host && $(CARGO) fmt --all

## host-rust-lint            Run 'clippy' on the 'host' source files.
.PHONY: host-rust-lint
lint: host-rust-lint
host-rust-lint:
	cd host && $(CARGO) clippy --no-deps --locked

## host-build-debug          Build the host executable for your current platform.
.PHONY: host-build-debug
build: host-build-debug
host-build-debug:
	cd host && $(CARGO) build

## host-build-release        Build the host executable for your current platform (release-ready).
.PHONY: host-build-release
build: host-build-release
host-build-release:
	cd host && $(CARGO) build --release

## host-build-release-all    Build the host executable for all supported platforms (release-ready).
.PHONY: host-build-release-all
build: host-build-release-all
host-build-release-all:


.PHONY: host-build-release-x86_64-unknown-linux-gnu
host-build-release-all: host-build-release-x86_64-unknown-linux-gnu
host-build-release-x86_64-unknown-linux-gnu: dependencies-cross
	cd host && $(CARGO_BIN)/cross build --target x86_64-unknown-linux-gnu --release

.PHONY: host-build-release-aarch64-unknown-linux-gnu
host-build-release-all: host-build-release-aarch64-unknown-linux-gnu
host-build-release-aarch64-unknown-linux-gnu:
	cd host && $(CARGO_BIN)/cross build --target aarch64-unknown-linux-gnu --release

.PHONY: host-build-release-x86_64-apple-darwin
# macOs builds not added to host-build-release-all, as it requires a custom
# Cross.toml image with the macOS SDK license.
host-build-release-x86_64-apple-darwin: dependencies-cross
	cd host && $(CARGO_BIN)/cross build --target x86_64-apple-darwin --release

.PHONY: host-build-release-x86_64-pc-windows-gnu
host-build-release-all: host-build-release-x86_64-pc-windows-gnu
host-build-release-x86_64-pc-windows-gnu: dependencies-cross
	cd host && $(CARGO_BIN)/cross build --target x86_64-pc-windows-gnu --release

.PHONY: dependencies-cross
dev-install: dependencies-cross
dependencies-cross:
	# cargo install cross --git https://github.com/cross-rs/cross
	cargo install cross


# ---------------------------------------------------------------------
# Container tasks

## dev-env-image             Build the dev-env image.
.PHONY: dev-env-image
build: dev-env-image
dev-env-image:
	cd containers/dev-env && $(DOCKER) build -t $(DEV_ENV_TAG) .

## dev-env-clean             Delete the local dev-env image.
.PHONY: dev-env-clean
clean: dev-env-clean
dev-env-clean:
	-docker rmi $(DEV_ENV_TAG)


## firewall-router-image     Build the firewall-router image.
.PHONY: firewall-router-image
build: firewall-router-image
firewall-router-image:
	cd containers/firewall-router && $(DOCKER) build -t $(FIREWALL_ROUTER_TAG) .

## firewall-router-clean     Delete the local firewall-router image.
.PHONY: firewall-router-clean
clean: firewall-router-clean
firewall-router-clean:
	-docker rmi $(FIREWALL_ROUTER_TAG)


## network-gateway-image     Build the network-gateway image.
.PHONY: network-gateway-image
build: network-gateway-image
network-gateway-image:
	cd containers/network-gateway && $(DOCKER) build -t $(NETWORK_GATEWAY_TAG) .

## network-gateway-clean     Delete the local network-gateway image.
.PHONY: network-gateway-clean
clean: network-gateway-clean
network-gateway-clean:
	-docker rmi $(NETWORK_GATEWAY_TAG)


# ---------------------------------------------------------------------
# Script tasks

## script-tests              Run the script unit tests.
#  This just runs tests, which doesn't use any dev requirements.
.PHONY: script-tests
test: script-tests
script-tests:
	$(PYTHON) -m unittest discover -t . -s scripts/tests


.PHONY: python-venv
dev-install: python-venv
python-venv:
	test -d .venv || $(PYTHON) -m venv .venv
	.venv/bin/pip install -r dev-requirements.txt


.PHONY: python-ruff
lint: python-ruff
python-ruff:
	.venv/bin/python -m ruff check scoter.py scripts/


.PHONY: python-bandit
lint: python-bandit
python-bandit:
	.venv/bin/python -m bandit -r scoter.py scripts/


# ---------------------------------------------------------------------
# Installation tasks

## installer                 Create the installation zip file.
.PHONY: installer
installer:

# TODO come up with an approach to handle all platforms,
# rather than a onsey-twosy.

.PHONY: mk-installer-x86_64-unknown-linux-gnu
installer: mk-installer-x86_64-unknown-linux-gnu
mk-installer-x86_64-unknown-linux-gnu: host-build-release-x86_64-unknown-linux-gnu
	bash -x build-scripts/mk-install.sh target/surf-scoter-x86_64-unknown-linux-gnu-1.0.0 x86_64-unknown-linux-gnu

## installer-clean           Clean the installation build generated files.
.PHONY: installer-clean
clean: installer-clean
installer-clean:
	-rm -r target/work
	-rm target/scoter-*.zip
