# The Envoy network gateway: Envoy's dynamic forward proxy, passing `CONNECT`
# tunnels only to the hosts and ports the egress rules allow.
#
# The distroless image: no shell, no package manager, and a non-root user
# (65532).  Pinned by the digest of its multi-platform index, so a moved tag
# upstream can't change what runs.  To update it, change both, and check the
# release notes for changes to the dynamic forward proxy.
FROM docker.io/envoyproxy/envoy:distroless-v1.36.12@sha256:7ced9068b113fb75c2436fda2b99943c67bc3c66ce6dc04b77df22249ce42f9e

# The configuration.  Surf Scoter mounts its own, made by
# `py/src/dist_bin/gateway_envoy.py` from the project's egress rules, over this one,
# which allows nothing.
COPY envoy.json /user-space/gateway/envoy.json

# Ensure the configuration works.
RUN ["/usr/local/bin/envoy", "--mode", "validate", "-c", "/user-space/gateway/envoy.json"]

# Unprivileged; the container drops every capability.
USER 65532:65532

EXPOSE 3128

# Two worker threads: they count against the container's `pids_limit`.  The
# access log is flushed every second (Envoy's default is ten), so
# `scoter netlog --follow` keeps up.  Envoy shuts down on SIGTERM, the
# default stop signal.
ENTRYPOINT ["/usr/local/bin/envoy", "-c", "/user-space/gateway/envoy.json", "--concurrency", "2", "--log-level", "warn", "--file-flush-interval-msec", "1000"]
CMD []
