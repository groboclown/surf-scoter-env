FROM public.ecr.aws/docker/library/debian:bookworm-slim

# Install squid + CA certs
RUN set -x \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
    squid ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Setup the squid configuration file.
COPY squid.conf /etc/squid/squid.conf

# The default allow list.
# Squid will read the list from /user-space/allowed-domains.txt, which should be mounted.
COPY allowed-domains.txt /etc/squid/allowed-domains.txt
# The block list isn't overwritable.
COPY blocked-destinations.txt /etc/squid/blocked-destinations.txt

# Ensure the configuration works.
RUN squid -k parse

# Start as the unprivileged user squid would otherwise switch to.  Switching
# needs the SETUID / SETGID capabilities, which the container drops.
USER proxy

EXPOSE 3128

CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
