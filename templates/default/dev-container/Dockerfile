# Starts from the distribution's dev container, which `scoter run` builds
# first (its `dev-env.yaml` names it as the parent).
FROM local-surf-scoter/dev-env:distro

# Install additional system packages.
USER root
RUN set -x \
 && apt-get update \
 && apt-get upgrade -y \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tmux \
 && rm -rf /var/lib/apt/lists/*

# End with user-mode installs and setup.
USER ubuntu
