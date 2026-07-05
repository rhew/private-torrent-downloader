#!/usr/bin/env python3

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request


DEFAULT_RPC_URL = "http://127.0.0.1:9091/transmission/rpc"
TORRENT_STATUS_STOPPED = 0


class TransmissionClient:
    def __init__(self, rpc_url):
        self.rpc_url = rpc_url
        self.session_id = None

    def request(self, method, arguments=None):
        body = json.dumps({
            "method": method,
            "arguments": arguments or {},
        }).encode("utf-8")

        for _ in range(2):
            request = urllib.request.Request(
                self.rpc_url,
                data=body,
                headers=self._headers(),
                method="POST",
            )

            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                if error.code == 409:
                    self.session_id = error.headers["X-Transmission-Session-Id"]
                    continue
                raise

            result = payload.get("result")
            if result != "success":
                raise RuntimeError(f"Transmission RPC {method} failed: {result}")
            return payload.get("arguments", {})

        raise RuntimeError("Transmission did not accept the RPC session id")

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.session_id:
            headers["X-Transmission-Session-Id"] = self.session_id
        return headers

    def get_torrents(self):
        return self.request("torrent-get", {
            "fields": [
                "id",
                "name",
                "status",
                "percentDone",
                "leftUntilDone",
            ],
        }).get("torrents", [])

    def stop_torrents(self, torrent_ids):
        if torrent_ids:
            self.request("torrent-stop", {"ids": torrent_ids})


def torrent_is_complete(torrent):
    if torrent.get("leftUntilDone") == 0:
        return True
    return torrent.get("percentDone") == 1


def complete_active_torrents(torrents):
    return [
        torrent
        for torrent in torrents
        if torrent_is_complete(torrent)
        and torrent.get("status") != TORRENT_STATUS_STOPPED
    ]


def env_int(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def log(message):
    print(message, flush=True)


def main():
    rpc_url = os.getenv("TRANSMISSION_RPC_URL", DEFAULT_RPC_URL)
    interval_seconds = env_int("CONTROLLER_POLL_SECONDS", 60)
    client = TransmissionClient(rpc_url)

    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log(f"Monitoring Transmission at {rpc_url}")
    while running:
        try:
            torrents = client.get_torrents()
            to_pause = complete_active_torrents(torrents)
            if to_pause:
                torrent_ids = [torrent["id"] for torrent in to_pause]
                names = ", ".join(torrent["name"] for torrent in to_pause)
                client.stop_torrents(torrent_ids)
                log(f"Paused {len(torrent_ids)} completed torrent(s): {names}")
            else:
                log("No completed active torrents found")
        except Exception as error:
            log(f"Controller check failed: {error}")

        deadline = time.monotonic() + interval_seconds
        while running and time.monotonic() < deadline:
            time.sleep(min(1, deadline - time.monotonic()))

    log("Controller stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
