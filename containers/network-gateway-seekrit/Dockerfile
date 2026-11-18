# The seekrit network gateway: seekrit-proxy, built from a pinned upstream
# commit with Surf Scoter's patches applied.
#
# The patches in ./patches are applied in file-name order.  Each one says what
# it does and what it prevents in its commit message.  They're kept as one file
# per change so each can be submitted upstream on its own.  To change one, work
# in a clone of the upstream at SEEKRIT_COMMIT with the patches applied as
# commits (`git am patches/*.patch`), then re-export them with:
#
#     git format-patch -q --no-signature -o patches SEEKRIT_COMMIT..HEAD
#
# The build stops if a patch doesn't apply cleanly.

# ---- build stage: fetch, patch, and build a static musl binary --------------
FROM public.ecr.aws/docker/library/rust:1.100.0-alpine3.24 AS build

# The upstream repository and the exact commit the patches are made against.
# Fetching by full commit hash, then checking it, means a moved branch or tag
# upstream can't change what gets built.
ARG SEEKRIT_REPO=https://github.com/seekritdev/proxy.git
ARG SEEKRIT_COMMIT=cfec9403c1984b35383fdf774b765e9fcf3730ad

# git to fetch and patch; musl-dev and a C toolchain to build `ring`.
RUN apk add --no-cache git musl-dev gcc make

WORKDIR /build
RUN set -x \
 && git init -q . \
 && git fetch -q --depth 1 "${SEEKRIT_REPO}" "${SEEKRIT_COMMIT}" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${SEEKRIT_COMMIT}"

COPY patches /patches
RUN set -x \
 && for patch in /patches/*.patch ; do \
        git apply --verbose "${patch}" || exit 1 ; \
    done

# --locked: the dependency set is exactly the committed Cargo.lock, as the
# patches leave it.
RUN cargo build --release --locked --bin seekrit-proxy

# ---- runtime stage: nothing but the binary -----------------------------------
# No OS and no shell: the gateway holds decrypted secrets in memory, so it
# gets as little else as possible.
FROM scratch

COPY --from=build /build/target/release/seekrit-proxy /seekrit-proxy

# Access log lines as JSON, for `scoter netlog`.  `seekrit_audit` records each
# secret injection by name; `seekrit_access` records each connection, request,
# and tunnel.  Neither ever carries a secret value.
ENV SEEKRIT_LOG_FORMAT=json \
    RUST_LOG=warn,seekrit_proxy=info,seekrit_audit=info,seekrit_access=info

# The proxy shuts down on SIGINT only.  As PID 1 it would ignore the default
# SIGTERM, and `stop` would wait out its timeout and then kill it.
STOPSIGNAL SIGINT

# A user with no entry in /etc/passwd (there is none): the proxy needs no
# privileges.  Surf Scoter sets the user in its compose file, so that the
# gateway can read the CA key it mounts.
USER 65532:65532

EXPOSE 3128

# Surf Scoter mounts the configuration that `py/src/dist_bin/gateway_seekrit.py`
# writes.  Without it, the proxy refuses to start.
ENTRYPOINT ["/seekrit-proxy", "--config", "/user-space/gateway/seekrit-proxy.toml"]
