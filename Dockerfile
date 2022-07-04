FROM alpine:3

RUN apk add --no-cache transmission-daemon \
    && mkdir -p /transmission/config \
    && chmod -R 1777 /transmission \
    && rm -rf /tmp/*

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/transmission-daemon", "--foreground", "--config-dir", "/transmission/config"]
