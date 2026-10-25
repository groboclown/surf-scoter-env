# The container that builds the program used to connect to the host.
FROM public.ecr.aws/docker/library/rust:slim-trixie AS build

WORKDIR /usr/src/surf-scoter-env
COPY Cargo.lock Cargo.toml ./
COPY src ./src
RUN set -x \
 && cargo test \
 && cargo build --release


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
 # Set up SSH settings.
 && mkdir -p /run/sshd /home/ubuntu/.ssh /home/ubuntu/.config/sshd \
 && chmod 700 /home/ubuntu/.ssh \
 && chmod 755 /home/ubuntu/.config /home/ubuntu/.config/sshd \
 && chown -R ubuntu:ubuntu \
        /home/ubuntu/.ssh \
        /home/ubuntu/.config \
        /home/ubuntu/.config/sshd \
 # The host commands' links to the bridge client, made at start-up by the
 # container's user (see entrypoint.sh), ahead of everything else on the PATH.
 && install -d -o ubuntu -g ubuntu -m 0755 /usr/local/scoter-bridge \
 # To have the /home magic point to the workspace, the user needs write
 # access to the /home directory.
 && chown ubuntu:ubuntu /home
ENV PATH=/usr/local/scoter-bridge:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

COPY --chown=ubuntu:ubuntu \
    sshd_config /home/ubuntu/.config/sshd/sshd_config.template
COPY --chmod=0555 entrypoint.sh /entrypoint.sh

USER ubuntu
WORKDIR /workspace

ENTRYPOINT [ "/entrypoint.sh" ]
