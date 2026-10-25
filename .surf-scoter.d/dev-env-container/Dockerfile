FROM local-surf-scoter-env/dev-env:build

# Install additional system packages.
#   Installs 'cargo' to get all the necessary dependencies.
USER root
RUN set -x \
 && apt-get update \
 && apt-get upgrade -y \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tmux \
        screen \
        dos2unix \
        zip \
        unzip \
        python3 \
        python3-venv \
        python3-pip \
        buildah \
        cargo \
        build-essential \
# Clean up installs.
 && rm -rf /var/lib/apt/lists/*

# End with user-mode installs and setup.
USER ubuntu

COPY --chown=ubuntu:ubuntu bashrc /tmp/bashrc

RUN set -x \
 # Add in extra helper goodies.
 # .. There is no ~/.bashrc, so just copy straight there.
 && mv /tmp/bashrc "${HOME}/.bashrc" \
 # Install user-land Rust, for Rust development.
 && curl https://sh.rustup.rs -sSf > /tmp/rust-installer \
 && chmod +x /tmp/rust-installer \
 && /tmp/rust-installer -y \
 # Ensure Rust runs.
 && "${HOME}/.cargo/bin/cargo" --version \
 # The ownership of the mounted /workspace directory will trigger git messages.
 && git config --global --add safe.directory /workspace
