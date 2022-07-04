# Private Torrent Downloader

Three pieces for now:

1. A script to select a Japanese VPN from VPN Gate
2. A Compose file to create the VPN with GlueTun and run Transmission 
3. A Docker line to serve media with miniDLNA

## Init

### Build the transmission image.

```
docker build -t transmission .
```

### Make directories we'll use for volume mounts.

```
mkdir downloads incomplete
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

<wait for download>

### issue: download is saved to /root/Downloads in container, not volume mounted at `/transmission/downloads`.

```
docker exec -it transmission-container cp /root/Downloads/* /transmission/downloads
```

## Serve Media using DLNA.

```
docker run -d \
  --net=host \
  -v /home/rhew/repos/download-image/downloads:/media \
  -e MINIDLNA_MEDIA_DIR=/media \
  -e MINIDLNA_FRIENDLY_NAME=X1DLNA \
  vladgh/minidlna
```
