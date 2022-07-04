# Private Torrent Downloader

Three pieces for now:

1. A script to select a Japanese VPN from VPN Gate
2. A Compose file to create the VPN with GlueTun and run Transmission 
3. A Docker line to serve media with miniDLNA

## Init

### Make the `downloads` directory (or update `docker-compose.yaml` to point to your existing directory).

```
mkdir downloads
```

## Run

### Create/update an `ovpn` file (`vpngate.ovpn`).

```
source .venv/bin/activate
./vpngate.py
```

### Start Transmission and GlueTun.

```
docker-compose up
```

## Serve media using DLNA.

```
docker run -d \
  --net=host \
  -v /home/rhew/repos/download-image/downloads:/media \
  -e MINIDLNA_MEDIA_DIR=/media \
  -e MINIDLNA_FRIENDLY_NAME=X1DLNA \
  vladgh/minidlna
```
