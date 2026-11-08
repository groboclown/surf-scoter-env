FROM public.ecr.aws/docker/library/debian:bookworm-slim

# Install Squid and CA certificates.
RUN set -x \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
    squid ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Set up the Squid configuration file.
COPY squid.conf /etc/squid/squid.conf

# The allowed hosts.  Surf Scoter mounts its own list over this one, which
# allows nothing; it's here so the configuration parses.
COPY egress-hosts.txt /user-space/egress-hosts.txt
# The block list isn't overwritable.
COPY blocked-destinations.txt /etc/squid/blocked-destinations.txt

# Ensure the configuration works.
RUN squid -k parse

# Start as the unprivileged user squid would otherwise switch to.  Switching
# needs the SETUID / SETGID capabilities, which the container drops.
USER proxy

EXPOSE 3128

CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
