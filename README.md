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
version: '3'
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

### Start Transmission, GlueTun and the minidlna server.

```
docker-compose up -d
```
