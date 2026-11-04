# Caddy, unchanged, but able to run as a non-root user with every capability dropped.
# The stock binary carries the file capability cap_net_bind_service; with all capabilities dropped
# and no-new-privileges, exec of such a binary fails ("Operation not permitted"). Low ports are
# bound through the net.ipv4.ip_unprivileged_port_start sysctl instead (see docker-compose.yml), so
# the file capability is removed. Its state lives in two directories this image creates for that
# user. (Directories declared as VOLUME in the stock image keep root
# ownership, so Caddy's data and config use their own paths, which compose mounts as volumes.)
FROM caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d
ENV XDG_DATA_HOME=/var/lib/caddy-data \
    XDG_CONFIG_HOME=/var/lib/caddy-config
RUN setcap -r /usr/bin/caddy \
    && mkdir -p /var/lib/caddy-data /var/lib/caddy-config \
    && chown -R 10001:10001 /var/lib/caddy-data /var/lib/caddy-config
