# Caddy, unchanged, but able to run as a non-root user: its state lives in two directories this
# image creates for that user. (Directories declared as VOLUME in the stock image keep root
# ownership, so Caddy's data and config use their own paths, which compose mounts as volumes.)
FROM caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d
ENV XDG_DATA_HOME=/var/lib/caddy-data \
    XDG_CONFIG_HOME=/var/lib/caddy-config
RUN mkdir -p /var/lib/caddy-data /var/lib/caddy-config \
    && chown -R 10001:10001 /var/lib/caddy-data /var/lib/caddy-config
