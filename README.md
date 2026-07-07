# Private Torrent Downloader and Media Server

Three pieces:

1. A script to select a VPN from VPN Gate
2. A Compose file to create the VPN with GlueTun, run Transmission, run Jackett, and run Jellyfin.
3. A `curator` TUI and optional Curator server. The server owns Jackett, Transmission, and filesystem operations; the TUI can run locally against that server from another machine.

## Media File Layout

By default the compose file mounts repo-local `downloads/` and `library/` directories. Override them with `DOWNLOADS_DIR` and `LIBRARY_DIR` in `.env` when the storage lives elsewhere.

```text
downloads/        # transmission stages
  incomplete/
  complete/
    books/
    isos/
    etc...

library/          # Jellyfin serves
  books/
  isos/
  etc...
```

## Networking Design

- Gluetun owns the VPN connection, using a generated VPNGate OpenVPN config.
- Transmission shares Gluetun's network namespace.
- Transmission ports are published on the Gluetun service, not the Transmission service.
- Gluetun's firewall is the fail-closed boundary.
- Jackett and Curator stay on the normal Docker network unless there is a clear reason to proxy them.
- Curator imports completed downloads from `downloads/` and writes managed media to `library/`.
- Jellyfin uses host networking for simpler discovery and serves the mounted `library/` tree.
- Expose admin UIs only where you intend to manage them.

## Init

### Create a local `.env` from the example and set UID, GID, mount paths, and VPNGate filters.

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
```

`APP_UID` and `APP_GID` are passed to LinuxServer containers as `PUID` and `PGID`. Set these when needed:

```dotenv
VPNGATE_MIN_SPEED=5000000
VPNGATE_MAX_PING=300
VPNGATE_MIN_UPTIME=86400
# VPNGATE_COUNTRY=US
VIDEO_GID=44
RENDER_GID=992
```


### Make the local data directories, or update `.env` to point to existing storage.

```
mkdir -p downloads library data/jellyfin/config data/jellyfin/cache gluetun
```

The directories mounted into containers must be writable by `APP_UID:APP_GID`. This matters for `downloads/`, `library/`, `jackett-config/`, and `data/jellyfin/`.

### Configure the Curator client

Start from the client example config on the machine where you run the TUI:

```bash
cp curator/client.example.toml curator/client.toml
```

Set the Curator server URL:

```toml
[curator]
server_url = "http://127.0.0.1:8787"
request_timeout = 25
```

- `curator.request_timeout` is how long the client will wait for the Curator server to answer.
- `network.timeout` is how long the client is asking the server to spend on Jackett/indexer work.

#### Add one or more media types

Add a `[media_types.<name>]` section to `curator/client.toml`. For example:

```toml
[media_types.isos]
label = "ISOs"
library_dir = "isos"
indexers = ["internetarchive", "linuxtracker"]
categories = [4020]
```

- `label` is the name UI will use for the media type.
- `library_dir` is where `m` archives completed downloads by default. Relative paths are resolved by the server under its configured library root.
- `indexers` are the Jackett indexer ids Curator will query. Look at the [indexer definition files for Jackett](https://github.com/Jackett/Jackett/tree/master/src/Jackett.Common/Definitions). The token you put in `curator/client.toml` is the definition filename without the `.yml` suffix.
- `categories` are numeric Jackett/Torznab category ids. See the [Jackett Categories Wiki](https://github.com/Jackett/Jackett/wiki/Jackett-Categories). Examples:
  - `2000` = `Movies`
  - `2040` = `Movies/HD`
  - `4020` = `PC/ISO`

### Configure the Curator server

Start from the server example config on the machine where Docker runs:

```bash
cp curator/server.example.toml curator/server.toml
```

## Run

### Generate the VPNGate config

```bash
python3 vpngate.py
```

This writes `gluetun/vpngate.ovpn` and `gluetun/vpngate-state.json`.

### Start Transmission, GlueTun, Jackett, Curator server, and Jellyfin.

```
docker compose up -d
```

### Refresh the VPNGate endpoint and recreate the VPN/transmission stack

```
./vpn-refresh
```

### Configure Jellyfin

Open `http://localhost:8096` on the Docker host, or replace `localhost` with the host name when accessing it remotely.

1. Finish the first-run setup.
2. Add library paths such as `/media/books` and `/media/isos`.
3. Enable the artwork and metadata options you want Jellyfin to store next to media files.
4. If equipped, enable Intel hardware transcoding in the admin dashboard.

```text
Dashboard -> Playback -> Transcoding
```

Enable hardware acceleration and select the Intel VA-API / Quick Sync option that Jellyfin offers for `/dev/dri`.

### Start the Curator TUI

```bash
python3 -m pip install -r requirements.txt
python3 -m curator
```

`python3 -m curator` uses `server_url` from `curator/client.toml`. You can override it from the command line:

```bash
python3 -m curator --server-url http://127.0.0.1:8787
```

## Problems and Solutions

### Lock the Transmission image for 32 bit ARM hosts.

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

### Fix Jellyfin bind mount ownership.

If Jellyfin logs `Access to the path '/config/log' is denied`, make `data/jellyfin` writable by `APP_UID:APP_GID`:

```bash
sudo chown -R "$(id -u):$(id -g)" data/jellyfin
```
