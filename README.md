# Private Torrent Downloader and Server

Two pieces:

1. A script to select a Japanese VPN from VPN Gate
2. A Compose file to create the VPN with GlueTun, run Transmission ,and run the miniDLNA server.

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

### Disable DoT for VPN Gate and use Cloudflare DoH over 443.

If VPN Gate times out with GlueTun DNS-over-TLS, add this local override:

```
services:
  gluetun:
    environment:
      - DNS_UPSTREAM_RESOLVER_TYPE=doh
      - DNS_UPSTREAM_RESOLVERS=cloudflare
```

### Start Transmission, GlueTun and the minidlna server.

```
docker-compose up -d
```
