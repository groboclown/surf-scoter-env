# Where the bridge programs' source (`rust/container` in the Surf Scoter
# repository) comes from:
#   git:   a sparse clone of that one directory, at the pinned commit below.
#          This is how the distribution builds it: the source isn't shipped
#          with the distribution.
#   local: a copy already in the build context, in `rust-container/`.
#          `make dev-env-image` stages one from your checkout
#          (build-scripts/stage-dev-env.sh), for Surf Scoter development.
ARG SCOTER_SOURCE=git


# The `git` source.  The commit is pinned by its full id, and checked after
# the fetch, so the source can't change under the same build arguments.
# When `rust/container` changes, set SCOTER_COMMIT to a commit with the
# change (`make dev-env-pin-check` warns when it's behind).
FROM public.ecr.aws/docker/library/rust:slim-trixie AS src-git
ARG SCOTER_REPO=https://github.com/groboclown/surf-scoter-env.git
ARG SCOTER_COMMIT=0000000000000000000000000000000000000000
RUN set -x \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git init -q /tmp/surf-scoter-env \
 && cd /tmp/surf-scoter-env \
 && git remote add origin "${SCOTER_REPO}" \
 && git sparse-checkout set --cone rust/container \
 && git fetch -q --depth 1 --filter=blob:none origin "${SCOTER_COMMIT}" \
 && git checkout -q FETCH_HEAD \
 && test "$( git rev-parse HEAD )" = "${SCOTER_COMMIT}" \
 && mv rust/container /usr/src/surf-scoter-env \
 && cd / \
 && rm -rf /tmp/surf-scoter-env


# The `local` source.
FROM public.ecr.aws/docker/library/rust:slim-trixie AS src-local
COPY rust-container /usr/src/surf-scoter-env


# Build the programs used to connect to the host.
FROM src-${SCOTER_SOURCE} AS build
WORKDIR /usr/src/surf-scoter-env
RUN set -x \
 && cargo test --locked \
 && cargo build --release --locked


# The base container.
# You'll create your own that builds on top of this.
FROM public.ecr.aws/docker/library/ubuntu:rolling

# Bring over the launcher.
# You will probably want to run `ln -s /usr/local/bin/scoter-client COMMON_NAME` on this.
COPY --chmod=0755 --from=build /usr/src/surf-scoter-env/target/release/client /usr/local/bin/scoter-client
COPY --chmod=0755 --from=build /usr/src/surf-scoter-env/target/release/broker /usr/local/bin/scoter-container

# Install system tools.
#   May want to switch to Dropbear SSH, or some userspace-only SSH server.
RUN set -x \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        # Required by the container.
        openssh-server \
        socat \
        ca-certificates \
        procps \
        gettext-base \
        # Basic developer tools.
        git \
        mercurial \
        curl \
        bash \
 # Clean up installs.
 && rm -rf /var/lib/apt/lists/* \
 # Create an unprivileged developer account.
 # (it already exists as user 'ubuntu')
 #&& useradd -m -u 1000 -s /bin/bash ubuntu \
 # Set up SSH settings.  The rendered configuration, the host key and the
 # authorized keys go in /run/scoter-sshd at each start: never in the home
 # directory, or the persistent store, where an earlier session could have
 # left its own.
 && mkdir -p /run/sshd \
 && install -d -o ubuntu -g ubuntu -m 0700 /run/scoter-sshd \
 # The trusted CAs with the network gateway's, made at each start.
 && install -d -o ubuntu -g ubuntu -m 0755 /run/scoter-ca \
 # The host commands' links to the bridge client, made at start-up by the
 # container's user (see entrypoint.sh), ahead of everything else on the PATH.
 && install -d -o ubuntu -g ubuntu -m 0755 /usr/local/scoter-bridge \
 # The mount points.  A new persistent store volume takes its owner from
 # the image's directory, so the user can write it.
 && install -d -o ubuntu -g ubuntu -m 0755 /project \
 && install -d -o ubuntu -g ubuntu -m 0700 /persistent-store
ENV PATH=/usr/local/scoter-bridge:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# The container's own configuration: root's, so the user can't change it.
# `persistent-store.txt` lists what in the home directory is kept in the
# persistent store between runs; everything else comes from the image on
# each start.  An image built on this one can add lines to it.
COPY --chmod=0644 sshd_config /etc/scoter/sshd_config.template
COPY --chmod=0644 persistent-store.txt /etc/scoter/persistent-store.txt
COPY --chmod=0555 entrypoint.sh /entrypoint.sh

USER ubuntu
WORKDIR /project

ENTRYPOINT [ "/entrypoint.sh" ]
