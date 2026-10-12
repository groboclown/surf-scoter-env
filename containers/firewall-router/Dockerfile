FROM public.ecr.aws/docker/library/alpine:3

# Install nftables for routing and firewall rules.
RUN set -x \
 && apk add --no-cache \
    nftables

COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY egress.nft /usr/local/etc/egress.nft

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
