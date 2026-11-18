# Simple makefile to have a unified place to see standard
# developer tasks.

CARGO ?= cargo
ZIP ?= zip
DOCKER ?= docker
CARGO_BIN ?= ~/.cargo/bin
PYTHON ?= python3
# Windows virtual environments put their programs under 'Scripts', not 'bin'.
ifeq ($(OS),Windows_NT)
VENV_BIN ?= Scripts
else
VENV_BIN ?= bin
endif
VENV_PYTHON ?= .venv/$(VENV_BIN)/python

# Used if you don't have osvscanner installed
GO ?= go

LOCAL_CONTAINER_TAG_PREFIX ?= local-surf-scoter-env/
DEV_ENV_TAG = $(LOCAL_CONTAINER_TAG_PREFIX)dev-env:build
FIREWALL_ROUTER_TAG = $(LOCAL_CONTAINER_TAG_PREFIX)firewall-router:build
NETWORK_GATEWAY_SQUID_TAG = $(LOCAL_CONTAINER_TAG_PREFIX)network-gateway-squid:build
NETWORK_GATEWAY_SEEKRIT_TAG = $(LOCAL_CONTAINER_TAG_PREFIX)network-gateway-seekrit:build
NETWORK_GATEWAY_ENVOY_TAG = $(LOCAL_CONTAINER_TAG_PREFIX)network-gateway-envoy:build

PROJECT_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

# See https://github.com/cdxgen/cdxgen#installing
# `make install-cdxgen` puts the standalone binary of this release here.
CDXGEN_VERSION ?= v13.6.0
CDXGEN := $(PROJECT_DIR)/target/sbom-gen/cdxgen
# See https://google.github.io/osv-scanner/installation/
OSVSCANNER := $$HOME/go/bin/osv-scanner

VERSION := $(file < version.txt)

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
##                           You may not want everything here.  See the CONTRIBUTING.md file for
##                           details.
.PHONY: dev-install
dev-install:


## installer                 Create the zip files for distribution.
.PHONY: installer
installer:

# ---------------------------------------------------------------------
# Running Surf Scoter on itself.  See CONTRIBUTING.md.

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

## container-rust-tests      Run unit tests for the `rust/container` tools (dev container bridge).
.PHONY: container-rust-tests
test: container-rust-tests
container-rust-tests:
	cd rust/container && $(CARGO) test --locked

## container-rust-clean      Clean the generated rust files from `rust/container`.
.PHONY: container-rust-clean
clean: container-rust-clean
container-rust-clean:
	-rm -rf rust/container/target

## container-rust-fmt        Run 'fmt' on the `rust/container` source files.
.PHONY: container-rust-fmt
fmt: container-rust-fmt
container-rust-fmt:
	cd rust/container && $(CARGO) fmt --all

## container-rust-lint       Run 'clippy' on the `rust/container` source files.
.PHONY: container-rust-lint
lint: container-rust-lint
container-rust-lint:
	cd rust/container && $(CARGO) clippy --no-deps --locked


## host-rust-tests           Run unit tests for the `rust/host` programs.
.PHONY: host-rust-tests
test: host-rust-tests
host-rust-tests:
	cd rust/host && $(CARGO) test --locked

## host-rust-clean           Clean the generated rust files from `rust/host`.
.PHONY: host-rust-clean
clean: host-rust-clean
host-rust-clean:
	-rm -rf rust/host/target

## host-rust-fmt             Run 'fmt' on the `rust/host` source files.
.PHONY: host-rust-fmt
fmt: host-rust-fmt
host-rust-fmt:
	cd rust/host && $(CARGO) fmt --all

## host-rust-lint            Run 'clippy' on the `rust/host` source files.
.PHONY: host-rust-lint
lint: host-rust-lint
host-rust-lint:
	cd rust/host && $(CARGO) clippy --no-deps --locked

## host-build-debug          Build the host executable for your current platform.
.PHONY: host-build-debug
build: host-build-debug
host-build-debug:
	cd rust/host && $(CARGO) build

## host-build-release        Build the host executable for your current platform (release-ready).
.PHONY: host-build-release
build: host-build-release
host-build-release:
	cd rust/host && $(CARGO) build --release

## host-build-release-all    Build the host executable for all supported platforms (release-ready).
.PHONY: host-build-release-all
build: host-build-release-all
host-build-release-all:

# Cross-build targets.
# TODO look at moving these to a method that allows for multiple targets
# without duplicating them out.
.PHONY: host-build-release-x86_64-unknown-linux-gnu
host-build-release-all: host-build-release-x86_64-unknown-linux-gnu
host-build-release-x86_64-unknown-linux-gnu: dependencies-cross
	cd rust/host && $(CARGO_BIN)/cross build --target x86_64-unknown-linux-gnu --release

.PHONY: host-build-release-aarch64-unknown-linux-gnu
host-build-release-all: host-build-release-aarch64-unknown-linux-gnu
host-build-release-aarch64-unknown-linux-gnu:
	cd rust/host && $(CARGO_BIN)/cross build --target aarch64-unknown-linux-gnu --release

.PHONY: host-build-release-x86_64-apple-darwin
# macOS builds not added to host-build-release-all, as it requires a custom
# Cross.toml image with the macOS SDK license.
host-build-release-x86_64-apple-darwin: dependencies-cross
	cd rust/host && $(CARGO_BIN)/cross build --target x86_64-apple-darwin --release

.PHONY: host-build-release-x86_64-pc-windows-gnu
host-build-release-all: host-build-release-x86_64-pc-windows-gnu
host-build-release-x86_64-pc-windows-gnu: dependencies-cross
	cd rust/host && $(CARGO_BIN)/cross build --target x86_64-pc-windows-gnu --release

.PHONY: dependencies-cross
dev-install: dependencies-cross
dependencies-cross:
	# cargo install cross --git https://github.com/cross-rs/cross
	cargo install cross


# ---------------------------------------------------------------------
# Container tasks

## image                     Build all included images.  Uses the internal tag name.
.PHONY: image
image:

## dev-env-image             Build the dev-env image, from this checkout's `rust/container`
##                           (copied into a staged build context, not fetched).
.PHONY: dev-env-image
build: dev-env-image
image: dev-env-image
dev-env-image:
	build-scripts/stage-dev-env.sh target/dev-env-context
	cd target/dev-env-context && $(DOCKER) build --build-arg SCOTER_SOURCE=local -t $(DEV_ENV_TAG) .

## dev-env-pin-check         Warn when `rust/container` has changed since the commit that
##                           containers/dev-env/Dockerfile fetches (SCOTER_COMMIT).
.PHONY: dev-env-pin-check
lint: dev-env-pin-check
dev-env-pin-check:
	@pin=$$( sed -n 's/^ARG SCOTER_COMMIT=//p' containers/dev-env/Dockerfile ) ; \
	if ! git cat-file -e "$${pin}^{commit}" 2>/dev/null ; then \
		echo "WARNING: containers/dev-env/Dockerfile SCOTER_COMMIT ($${pin}) isn't a commit here." ; \
	elif ! git diff --quiet "$${pin}" -- rust/container ; then \
		echo "WARNING: rust/container changed since SCOTER_COMMIT ($${pin}); update containers/dev-env/Dockerfile." ; \
	fi

## dev-env-clean             Delete the local dev-env image.
.PHONY: dev-env-clean
clean: dev-env-clean
dev-env-clean:
	-$(DOCKER) rmi $(DEV_ENV_TAG)
	-rm -rf target/dev-env-context


## firewall-router-image     Build the firewall-router image.
.PHONY: firewall-router-image
build: firewall-router-image
image: firewall-router-image
firewall-router-image:
	cd containers/firewall-router && $(DOCKER) build -t $(FIREWALL_ROUTER_TAG) .

## firewall-router-clean     Delete the local firewall-router image.
.PHONY: firewall-router-clean
clean: firewall-router-clean
firewall-router-clean:
	-$(DOCKER) rmi $(FIREWALL_ROUTER_TAG)


## network-gateway-squid-image Build the Squid network-gateway image.
.PHONY: network-gateway-squid-image
build: network-gateway-squid-image
image: network-gateway-squid-image
network-gateway-squid-image:
	cd containers/network-gateway-squid && $(DOCKER) build -t $(NETWORK_GATEWAY_SQUID_TAG) .

## network-gateway-squid-clean Delete the local Squid network-gateway image.
.PHONY: network-gateway-squid-clean
clean: network-gateway-squid-clean
network-gateway-squid-clean:
	-$(DOCKER) rmi $(NETWORK_GATEWAY_SQUID_TAG)

## network-gateway-seekrit-image Build the seekrit network-gateway image.
.PHONY: network-gateway-seekrit-image
build: network-gateway-seekrit-image
image: network-gateway-seekrit-image
network-gateway-seekrit-image:
	cd containers/network-gateway-seekrit && $(DOCKER) build -t $(NETWORK_GATEWAY_SEEKRIT_TAG) .

## network-gateway-seekrit-clean Delete the local seekrit network-gateway image.
.PHONY: network-gateway-seekrit-clean
clean: network-gateway-seekrit-clean
network-gateway-seekrit-clean:
	-$(DOCKER) rmi $(NETWORK_GATEWAY_SEEKRIT_TAG)

## network-gateway-envoy-image Build the Envoy network-gateway image.
.PHONY: network-gateway-envoy-image
build: network-gateway-envoy-image
image: network-gateway-envoy-image
network-gateway-envoy-image:
	cd containers/network-gateway-envoy && $(DOCKER) build -t $(NETWORK_GATEWAY_ENVOY_TAG) .

## network-gateway-envoy-clean Delete the local Envoy network-gateway image.
.PHONY: network-gateway-envoy-clean
clean: network-gateway-envoy-clean
network-gateway-envoy-clean:
	-$(DOCKER) rmi $(NETWORK_GATEWAY_ENVOY_TAG)

## envoy-secrets-tests       Run unit tests for the Envoy gateway's secrets filter and envoy-inject.
.PHONY: envoy-secrets-tests
test: envoy-secrets-tests
envoy-secrets-tests:
	cd containers/network-gateway-envoy/secrets && $(CARGO) test --locked

## envoy-secrets-lint        Run 'clippy' on the Envoy gateway's secrets filter (host and Wasm) and envoy-inject.
.PHONY: envoy-secrets-lint
lint: envoy-secrets-lint
envoy-secrets-lint:
	cd containers/network-gateway-envoy/secrets && $(CARGO) clippy --no-deps --locked --all-targets
	cd containers/network-gateway-envoy/secrets && $(CARGO) clippy --no-deps --locked -p secrets-filter --target wasm32-unknown-unknown

## envoy-secrets-fmt         Run 'fmt' on the Envoy gateway's secrets filter and envoy-inject.
.PHONY: envoy-secrets-fmt
fmt: envoy-secrets-fmt
envoy-secrets-fmt:
	cd containers/network-gateway-envoy/secrets && $(CARGO) fmt --all

## envoy-secrets-clean       Clean the generated rust files from the Envoy gateway's secrets filter.
.PHONY: envoy-secrets-clean
clean: envoy-secrets-clean
envoy-secrets-clean:
	-rm -rf containers/network-gateway-envoy/secrets/target


# ---------------------------------------------------------------------
# Script tasks

## script-tests              Run the Python unit tests (`py/tests`).
#  This just runs tests, which doesn't use any dev requirements.
.PHONY: script-tests
test: script-tests
script-tests:
	mkdir -p target
	cd py && $(VENV_PYTHON) -m coverage run -m pytest --junitxml=../target/python-test-results.xml


.PHONY: python-venv
dev-install: python-venv
python-venv:
	test -d py/.venv || $(PYTHON) -m venv py/.venv
	py/.venv/$(VENV_BIN)/python -m pip install -r py/dev-requirements.txt


## python-ruff               Run ruff on the Python sources (settings in py/pyproject.toml).
.PHONY: python-ruff
lint: python-ruff
python-ruff:
	cd py && $(VENV_PYTHON) -m ruff check src tests


## python-bandit             Run bandit on the Python sources (settings in py/pyproject.toml).
.PHONY: python-bandit
lint: python-bandit
python-bandit:
	cd py && $(VENV_PYTHON) -m bandit -c pyproject.toml -r src tests


# ---------------------------------------------------------------------
# Installation tasks

# TODO come up with an approach to handle all platforms,
# rather than a onsey-twosy.

.PHONY: mk-installer-x86_64-unknown-linux-gnu
installer: mk-installer-x86_64-unknown-linux-gnu
mk-installer-x86_64-unknown-linux-gnu: host-build-release-x86_64-unknown-linux-gnu
	sh -x build-scripts/mk-install.sh target/surf-scoter-x86_64-unknown-linux-gnu-$(VERSION) x86_64-unknown-linux-gnu

## installer-clean           Clean the installation build generated files.
.PHONY: installer-clean
clean: installer-clean
installer-clean:
	-rm target/installer


# ---------------------------------------------------------------------
# Bill of Materials and related security tasks

## sbom                      Generate the Software Bill of Materials report
##                           This requires an installation of 'cdxgen' to use.
# Currently, the Python libraries have no dependencies other than Python std lib.
.PHONY: sbom
sbom:


sbom: target/sboms/surf-scoter-host.cdx.json
target/sboms/surf-scoter-host.cdx.json: target/sboms
	$(CDXGEN) -t rust --no-recurse --format cyclonedx \
		--author "Surf Scoter Team" \
		--output $@ \
		rust/host

sbom: target/sboms/surf-scoter-bridge.cdx.json
target/sboms/surf-scoter-bridge.cdx.json: target/sboms
	$(CDXGEN) -t rust --no-recurse --format cyclonedx \
		--author "Surf Scoter Team" \
		--output $@ \
		rust/container

sbom: target/sboms/surf-scoter-python-dev.cdx.json
# cdxgen expects a directory for python, not a file.
target/sboms/surf-scoter-python-dev.cdx.json: target/sboms
	$(CDXGEN) -t python --no-recurse --format cyclonedx \
		--author "Surf Scoter Team" \
		--project-version $(VERSION) \
		--project-name "surf-scoter Python dev tools" \
		--output $@ \
		py

sbom: target/sboms/surf-scoter-dev-env.cdx.json
target/sboms/surf-scoter-dev-env.cdx.json: target/images/dev-env.tar
	$(CDXGEN) -t docker --format cyclonedx \
		--author "Surf Scoter Team" \
		--project-version $(VERSION) \
		--project-name "surf-scoter dev-env" \
		--output $@ \
		$<

target/images/dev-env.tar: dev-env-image target/images
	test -f $@ && rm $@ || true
	$(DOCKER) save -o $@ $(DEV_ENV_TAG)

sbom: target/sboms/surf-scoter-firewall-router.cdx.json
target/sboms/surf-scoter-firewall-router.cdx.json: target/images/firewall-router.tar
	$(CDXGEN) -t docker --format cyclonedx \
		--author "Surf Scoter Team" \
		--project-version $(VERSION) \
		--project-name "surf-scoter firewall-router" \
		--output $@ \
		$<

target/images/firewall-router.tar: firewall-router-image target/images
	test -f $@ && rm $@ || true
	$(DOCKER) save -o $@ $(FIREWALL_ROUTER_TAG)

sbom: target/sboms/surf-scoter-network-gateway-squid.cdx.json
target/sboms/surf-scoter-network-gateway-squid.cdx.json: target/images/network-gateway-squid.tar
	$(CDXGEN) -t docker --format cyclonedx \
		--author "Surf Scoter Team" \
		--project-version $(VERSION) \
		--project-name "surf-scoter network-gateway-squid" \
		--output $@ \
		$<

target/images/network-gateway-squid.tar: network-gateway-squid-image target/images
	test -f $@ && rm $@ || true
	$(DOCKER) save -o $@ $(NETWORK_GATEWAY_SQUID_TAG)

sbom: target/sboms/surf-scoter-network-gateway-seekrit.cdx.json
target/sboms/surf-scoter-network-gateway-seekrit.cdx.json: target/images/network-gateway-seekrit.tar
	$(CDXGEN) -t docker --format cyclonedx \
		--author "Surf Scoter Team" \
		--project-version $(VERSION) \
		--project-name "surf-scoter network-gateway-seekrit" \
		--output $@ \
		$<

target/images/network-gateway-seekrit.tar: network-gateway-seekrit-image target/images
	test -f $@ && rm $@ || true
	$(DOCKER) save -o $@ $(NETWORK_GATEWAY_SEEKRIT_TAG)

sbom: target/sboms/surf-scoter-network-gateway-envoy.cdx.json
target/sboms/surf-scoter-network-gateway-envoy.cdx.json: target/images/network-gateway-envoy.tar
	$(CDXGEN) -t docker --format cyclonedx \
		--author "Surf Scoter Team" \
		--project-version $(VERSION) \
		--project-name "surf-scoter network-gateway-envoy" \
		--output $@ \
		$<

target/images/network-gateway-envoy.tar: network-gateway-envoy-image target/images
	test -f $@ && rm $@ || true
	$(DOCKER) save -o $@ $(NETWORK_GATEWAY_ENVOY_TAG)

target/sboms:
	mkdir -p target/sboms
target/images:
	mkdir -p target/images

## install-cdxgen            Download the standalone cdxgen binary (CDXGEN_VERSION) into target/sbom-gen.
.PHONY: install-cdxgen
dev-install: install-cdxgen
install-cdxgen:
	build-scripts/install-cdxgen.sh "$(CDXGEN_VERSION)" target/sbom-gen


## scan                      Scan the SBOM files for issues.  Requires `osv-scanner`.
# Scanning SBOMs with osv-scanner requires an exact filename.
.PHONY: scan
sbom: scan
scan: target/sboms/surf-scoter-host.scan.json
target/sboms/surf-scoter-host.scan.json: target/sboms/surf-scoter-host.cdx.json
	-$(OSVSCANNER) scan source \
		--lockfile $< \
		--format json \
		--output-file $@
	$(OSVSCANNER) scan source \
		--lockfile $<

scan: target/sboms/surf-scoter-bridge.scan.json
target/sboms/surf-scoter-bridge.scan.json: target/sboms/surf-scoter-bridge.cdx.json
	-$(OSVSCANNER) scan source \
		--lockfile $< \
		--format json \
		--output-file $@
	$(OSVSCANNER) scan source \
		--lockfile $<

# This doesn't work as expected.
# osv-scanner starts walking /, and fails
#scan: target/sboms/surf-scoter-python-dev.scan.json
target/sboms/surf-scoter-python-dev.scan.json: target/sboms/surf-scoter-python-dev.cdx.json
	-$(OSVSCANNER) scan source \
		--lockfile $< \
		--format json \
		--output-file $@
	$(OSVSCANNER) scan source \
		--lockfile $<

scan: target/sboms/surf-scoter-dev-env.scan.json
target/sboms/surf-scoter-dev-env.scan.json: target/sboms/surf-scoter-dev-env.cdx.json
	-$(OSVSCANNER) scan source \
		--lockfile $< \
		--format json \
		--output-file $@
	$(OSVSCANNER) scan source \
		--lockfile $<

scan: target/sboms/surf-scoter-firewall-router.scan.json
target/sboms/surf-scoter-firewall-router.scan.json: target/sboms/surf-scoter-firewall-router.cdx.json
	-$(OSVSCANNER) scan source \
		--lockfile $< \
		--format json \
		--output-file $@
	$(OSVSCANNER) scan source \
		--lockfile $<

scan: target/sboms/surf-scoter-network-gateway-squid.scan.json
target/sboms/surf-scoter-network-gateway-squid.scan.json: target/sboms/surf-scoter-network-gateway-squid.cdx.json
	-$(OSVSCANNER) scan source \
		--lockfile $< \
		--format json \
		--output-file $@
	$(OSVSCANNER) scan source \
		--lockfile $<

scan: target/sboms/surf-scoter-network-gateway-seekrit.scan.json
target/sboms/surf-scoter-network-gateway-seekrit.scan.json: target/sboms/surf-scoter-network-gateway-seekrit.cdx.json
	-$(OSVSCANNER) scan source \
		--lockfile $< \
		--format json \
		--output-file $@
	$(OSVSCANNER) scan source \
		--lockfile $<

scan: target/sboms/surf-scoter-network-gateway-envoy.scan.json
target/sboms/surf-scoter-network-gateway-envoy.scan.json: target/sboms/surf-scoter-network-gateway-envoy.cdx.json
	-$(OSVSCANNER) scan source \
		--lockfile $< \
		--format json \
		--output-file $@
	$(OSVSCANNER) scan source \
		--lockfile $<


.PHONY: install-osvscanner
dev-install: install-osvscanner
install-osvscanner:
	go install github.com/google/osv-scanner/v2/cmd/osv-scanner@latest
