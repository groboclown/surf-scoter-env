FROM public.ecr.aws/docker/library/alpine:3

# Upgrade the base image's packages (it can lag behind security fixes in the
# Alpine repositories), install Squid, then remove the package manager.
# Nothing installs packages at runtime, and removing apk-tools also removes
# what only it needs, so the image carries less code that could have
# vulnerabilities.  Squid only tunnels CONNECT requests and never checks
# certificates, so it needs no CA certificates.
RUN set -x \
 && apk upgrade --no-cache \
 && apk add --no-cache \
    squid \
 && apk del --no-cache apk-tools \
 && rm -rf /etc/apk /usr/share/apk /var/cache/apk
# /lib/apk/db stays: it lists the installed packages, which the SBOM (and the
# vulnerability scan) reads.

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
USER squid

EXPOSE 3128

CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
