# Private Torrent Downloader and Media Server

Three pieces:

1. A script to select a VPN from VPN Gate
2. A Compose file to create the VPN with GlueTun, run Transmission, run Jackett, and run the miniDLNA server.
3. A `curator` TUI that reads Jackett's generated API key, queries configured indexers, and sends selected results to Transmission.

## Init

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


### Make the `downloads` and `library` directories (or update `docker-compose.yml` to point to your existing directory).

```
mkdir downloads library
```

### Configure the Curator

Start from the example config:

```bash
cp curator/config.example.toml curator/config.toml
```

#### Add one or more media types

Add a `[media_types.<name>]` section to `curator/config.toml`. For example:

```toml
[media_types.isos]
label = "ISOs"
library_dir = "../library/isos"
indexers = ["internetarchive", "linuxtracker"]
categories = [4020]
```

- `label` is the name UI will use for the media type.
- `library_dir` is where `m` archives completed downloads by default
- `indexers` are the Jackett indexer ids Curator will query. See "How to find available indexers" below. In this example: `internetarchive`, `linuxtracker`.
- `categories` are numeric Jackett/Torznab category ids. See the [Jackett Categories Wiki](https://github.com/Jackett/Jackett/wiki/Jackett-Categories). Examples:
  - `2000` = `Movies`
  - `2040` = `Movies/HD`
  - `4020` = `PC/ISO`

### How to find available indexers

Jackett keeps one definition file per indexer [in its repository](https://github.com/Jackett/Jackett/tree/master/src/Jackett.Common/Definitions). The token you put in `curator/config.toml` is the definition filename without
the `.yml` suffix. Examples:

- `internetarchive.yml` -> `internetarchive`
- `linuxtracker.yml` -> `linuxtracker`

Current shipped examples also include:

- `bibliotik.yml` -> `bibliotik`
- `booktracker.yml` -> `booktracker`
- `audionews.yml` -> `audionews`

The example config includes `books` and `audiobooks` media types. The shipped
defaults use public sources so those profiles work without extra account setup:

- `books` uses `internetarchive`
- `audiobooks` uses `internetarchive`

If you want closer parity with dedicated book and audiobook trackers, the
closest Jackett-supported matches I confirmed upstream are:

- `bibliotik` for ebooks and audiobooks
- `booktracker` for books
- `audionews` for audiobooks

Those require extra setup and are not good zero-config defaults.

I did not confirm current Jackett definitions for `audiobookbay` or Anna's
Archive, so those are not in the shipped config.

## Run

### Create/update an `ovpn` file (`vpngate.ovpn`).

```
python3 vpngate.py
```

### Start Transmission, GlueTun, Jackett, and the minidlna server.

```
docker-compose up -d
```

### Start the Curator TUI

```bash
python3 -m pip install -r requirements.txt
python3 -m curator
```

## Problems

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
