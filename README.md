# Private Torrent Downloader and Server

Four pieces:

1. A script to select a Japanese VPN from VPN Gate
2. A Compose file to create the VPN with GlueTun, run Transmission, run Jackett, and run the miniDLNA server.
3. A `curator` TUI that reads Jackett's generated API key, queries configured indexers, and sends selected results to Transmission.
4. A TOML config that defines media types, indexers, Transmission RPC, and library destinations.

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

### Create a local `.env` from the example and set UID, GID, and mount paths.

```bash
cp .env.example .env
```

Set at least:

```dotenv
APP_UID=1000
APP_GID=1000
DOWNLOADS_DIR=./downloads
LIBRARY_DIR=./library
JACKETT_CONFIG_DIR=./jackett-config
MINIDLNA_FRIENDLY_NAME=MyCoolMedia
```

For Ansible, template the same `.env` file with the target host's UID/GID and
mount paths. That keeps Compose itself unchanged across hosts.

### Optional: local Compose overrides for host-specific behavior. Example:

```
cat << EOF
services:
  gluetun:
    environment:
      - DNS_UPSTREAM_RESOLVER_TYPE=doh
      - DNS_UPSTREAM_RESOLVERS=cloudflare
EOF
```

### Lock the Transmission image per host.

The compose file defaults to `lscr.io/linuxserver/transmission`, which works for regular x86_64 hosts like an i5 server. On the Pi, set this in `.env` to pin the 32-bit image:

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

### Start Transmission, GlueTun, Jackett, and the minidlna server.

```
docker-compose up -d
```

For keyboard-driven search, sorting, and Transmission add/progress controls,
install the Python dependencies and run:

```bash
python3 -m pip install -r requirements.txt
python3 -m curator
```

The default config lives at [curator/config.toml](/home/rhew/repos/private-torrent-downloader/curator/config.toml).
It defines the Jackett URL, the Transmission RPC URL, the Jackett base URL as
seen from Transmission, the network timeout, the default media type, the
configured indexers for each media type, and each media type's library
directory.

Override the config path with:

```bash
python3 -m curator --config path/to/config.toml
```
