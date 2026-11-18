# The Envoy network gateway: Envoy's dynamic forward proxy, passing `CONNECT`
# tunnels only to the hosts and ports the egress rules allow, and reading the
# `https` rules' hosts, with their secrets filled in.

# ---- build stage: the secrets filter (Wasm) and envoy-inject ----------------
# Surf Scoter's own code, in ./secrets: the filter that fills in secrets, and
# the client `scoter` gives them to.  `--locked`: the dependencies are exactly
# the committed Cargo.lock.  The filter targets `wasm32-unknown-unknown`, as
# proxy-wasm 0.2's start-up code doesn't link with `wasm32-wasip1`'s.
FROM public.ecr.aws/docker/library/rust:1.100.0-alpine3.24 AS build

RUN apk add --no-cache musl-dev \
 && rustup target add wasm32-unknown-unknown

WORKDIR /build
COPY secrets /build
RUN cargo test --locked --release \
 && cargo build --locked --release -p secrets-filter --target wasm32-unknown-unknown \
 && cargo build --locked --release -p envoy-inject

# ---- runtime stage -------------------------------------------------------------
# The distroless image: no shell, no package manager, and a non-root user
# (65532).  Pinned by the digest of its multi-platform index, so a moved tag
# upstream can't change what runs.  To update it, change both, and check the
# release notes for changes to the dynamic forward proxy and to Wasm (the
# secrets filter needs the V8 runtime, and shared data across VMs).
FROM docker.io/envoyproxy/envoy:distroless-v1.36.12@sha256:7ced9068b113fb75c2436fda2b99943c67bc3c66ce6dc04b77df22249ce42f9e

# The secrets filter, which `gateway_envoy.py` names (`SECRETS_FILTER`), and
# envoy-inject (static, musl), which `gateway_postrun_envoy.py` runs.
COPY --from=build /build/target/wasm32-unknown-unknown/release/secrets_filter.wasm /usr/local/lib/surf-scoter/secrets_filter.wasm
COPY --from=build /build/target/release/envoy-inject /usr/local/bin/envoy-inject

# The configuration.  Surf Scoter mounts its own, made by
# `py/src/dist_bin/gateway_envoy.py` from the project's egress rules, over this one,
# which allows nothing.
COPY envoy.json /user-space/gateway/envoy.json

# Ensure the configuration works, and that the secrets filter loads in this
# Envoy (`validate-secrets.json`: only its inject listener).
COPY validate-secrets.json /tmp/validate-secrets.json
RUN ["/usr/local/bin/envoy", "--mode", "validate", "-c", "/user-space/gateway/envoy.json"]
RUN ["/usr/local/bin/envoy", "--mode", "validate", "-c", "/tmp/validate-secrets.json"]

# Unprivileged; the container drops every capability.
USER 65532:65532

EXPOSE 3128

# Two worker threads: they count against the container's `pids_limit`.  The
# access log is flushed every second (Envoy's default is ten), so
# `scoter netlog --follow` keeps up.  Envoy shuts down on SIGTERM, the
# default stop signal.
ENTRYPOINT ["/usr/local/bin/envoy", "-c", "/user-space/gateway/envoy.json", "--concurrency", "2", "--log-level", "warn", "--file-flush-interval-msec", "1000"]
CMD []
