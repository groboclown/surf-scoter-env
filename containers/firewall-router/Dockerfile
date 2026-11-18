FROM public.ecr.aws/docker/library/alpine:3

# Upgrade the base image's packages (it can lag behind security fixes in the
# Alpine repositories), install nftables for routing and firewall rules, then
# remove the package manager.  Nothing installs packages at runtime, and
# removing apk-tools also removes what only it needs (libapk, zlib), so the
# image carries less code that could have vulnerabilities.
RUN set -x \
 && apk upgrade --no-cache \
 && apk add --no-cache \
    nftables \
 && apk del --no-cache apk-tools \
 && rm -rf /etc/apk /usr/share/apk /var/cache/apk
# /lib/apk/db stays: it lists the installed packages, which the SBOM (and the
# vulnerability scan) reads.

COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY egress.nft /usr/local/etc/egress.nft

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
