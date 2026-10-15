FROM local-surf-scoter-env/dev-env:build

# Install additional system packages.
USER root
RUN set -x \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tmux \
        screen \
        dos2unix \
        zip \
        unzip \
        python3 \
        python3-venv \
        python3-pip \
 # Clean up installs.
 && rm -rf /var/lib/apt/lists/*

# End with user-mode installs and setup.
USER ubuntu

COPY --chown=ubuntu:ubuntu bashrc /tmp/bashrc

RUN set -x \
 # Add in extra helper goodies.
 && cat $HOME/.bashrc /tmp/bashrc > /tmp/b \
 && mv /tmp/b $HOME/.bashrc \
 && rm /tmp/bashrc \
 # Install user-land rust, for rust development.
 && curl https://sh.rustup.rs -sSf | sh
