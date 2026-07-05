# Private Torrent Downloader and Server

Three pieces:

1. A script to select a Japanese VPN from VPN Gate
2. A Compose file to create the VPN with GlueTun, run Transmission, run the controller, and run the miniDLNA server.
3. A controller image that monitors Transmission and pauses completed torrents that are still active.

## Init

### Make the `downloads` directory (or update `docker-compose.yaml` to point to your existing directory).

```
mkdir downloads
```

## Run

### Create/update an `ovpn` file (`vpngate.ovpn`).

```
python3 vpngate.py
```

### Configure UID, GID, and download/media mount to use. Example:

```
cat << EOF
services:
  transmission:
    environment:
      - PUID=1001
      - PGID=1001
    volumes:
      - /mnt/yourdevice/downloads:/downloads
  minidlna:
    environment:
      - MINIDLNA_FRIENDLY_NAME=MyCoolMedia
    volumes:
      - /mnt/yourdevice/downloads:/media
EOF
```

### Lock the Transmission image per host.

The compose file defaults to `lscr.io/linuxserver/transmission`, which works for regular x86_64 hosts like an i5 server. On the Pi, create a local `.env` file next to `docker-compose.yml` to pin the 32-bit image:

```
TRANSMISSION_IMAGE=linuxserver/transmission:arm32v7-4.0.3
```

### Disable DoT for VPN Gate and use Cloudflare DoH over 443.

If VPN Gate times out with GlueTun DNS-over-TLS, add this local override:

```
services:
  gluetun:
    environment:
      - DNS_UPSTREAM_RESOLVER_TYPE=doh
      - DNS_UPSTREAM_RESOLVERS=cloudflare
```

### Start Transmission, GlueTun, the controller, and the minidlna server.

```
docker-compose up -d
```

The controller calls Transmission's RPC API every 60 seconds by default. Override
`CONTROLLER_POLL_SECONDS` in `docker-compose.override.yml` to change the polling interval.
