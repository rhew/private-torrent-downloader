from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


JACKETT_CONFIG_FILE = Path("ServerConfig.json")
SEARCH_MODE = "search"


@dataclass(frozen=True)
class SearchResult:
    indexer: str
    title: str
    size: str
    seeders: str
    leechers: str
    category: str
    guid: str
    link: str

    @property
    def identity(self) -> str:
        key = self.guid or self.link or f"{normalize_title(self.title)}|{self.size}"
        return f"{self.indexer}|{key}"

    @property
    def seeders_value(self) -> int:
        return parse_int(self.seeders)

    @property
    def leechers_value(self) -> int:
        return parse_int(self.leechers)


def load_api_key(config_dir: Path) -> str:
    jackett_config_file = config_dir / JACKETT_CONFIG_FILE
    data = json.loads(jackett_config_file.read_text(encoding="utf-8"))
    api_key = data.get("APIKey")
    if not api_key:
        raise KeyError(f"APIKey not found in {jackett_config_file}")
    return api_key


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"downloads": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"downloads": data.get("downloads", {})}


def save_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def mark_download(state: dict, result: SearchResult, torrent: dict) -> None:
    state["downloads"][result.identity] = {
        "indexer": result.indexer,
        "title": result.title,
        "size": result.size,
        "guid": result.guid,
        "link": result.link,
        "torrent_id": torrent.get("id"),
        "torrent_name": torrent.get("name"),
        "hash_string": torrent.get("hashString"),
        "download_dir": torrent.get("downloadDir"),
        "status": "added",
    }


def update_download_progress(state: dict, torrents: list[dict]) -> None:
    by_id = {torrent.get("id"): torrent for torrent in torrents}
    by_hash = {
        torrent.get("hashString"): torrent
        for torrent in torrents
        if torrent.get("hashString")
    }
    for download in state["downloads"].values():
        torrent = None
        torrent_id = download.get("torrent_id")
        if torrent_id is not None:
            torrent = by_id.get(torrent_id)
        if torrent is None and download.get("hash_string"):
            torrent = by_hash.get(download["hash_string"])
        if torrent is None:
            continue

        download["torrent_id"] = torrent.get("id")
        download["torrent_name"] = torrent.get("name")
        download["hash_string"] = torrent.get("hashString")
        download["download_dir"] = torrent.get("downloadDir")
        download["percent_done"] = torrent.get("percentDone")
        download["left_until_done"] = torrent.get("leftUntilDone")
        download["rate_download"] = torrent.get("rateDownload")
        download["status_code"] = torrent.get("status")
        download["status"] = describe_torrent_status(torrent)


def describe_torrent_status(torrent: dict) -> str:
    status = torrent.get("status")
    if torrent.get("leftUntilDone") == 0 or torrent.get("percentDone") == 1:
        return "complete"
    if status == 0:
        return "paused"
    percent_done = torrent.get("percentDone")
    if isinstance(percent_done, (int, float)):
        return f"{percent_done:.0%}"
    return "active"


def build_search_url(base_url: str, api_key: str, indexer: str, query: str) -> str:
    params = urllib.parse.urlencode({
        "apikey": api_key,
        "t": SEARCH_MODE,
        "q": query,
    })
    return f"{base_url.rstrip('/')}/api/v2.0/indexers/{indexer}/results/torznab/api?{params}"


def fetch_xml(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "private-torrent-downloader/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def search_indexer(
    query: str,
    indexer: str,
    api_key: str,
    base_url: str,
    timeout: float,
) -> list[SearchResult]:
    url = build_search_url(base_url, api_key, indexer, query)
    return list(parse_items(fetch_xml(url, timeout), indexer))


def search_indexers(
    query: str,
    indexers: tuple[str, ...],
    api_key: str,
    base_url: str,
    timeout: float,
) -> tuple[list[SearchResult], dict[str, str]]:
    results: list[SearchResult] = []
    errors: dict[str, str] = {}
    for indexer in indexers:
        try:
            results.extend(search_indexer(query, indexer, api_key, base_url, timeout))
        except urllib.error.HTTPError as error:
            errors[indexer] = f"HTTP {error.code}"
        except Exception as error:
            errors[indexer] = str(error)
    return results, errors


def format_size(size_bytes) -> str:
    try:
        size = float(size_bytes)
    except (TypeError, ValueError):
        return "?"

    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return "?"


def parse_items(payload: bytes, indexer: str):
    root = ET.fromstring(payload)
    for item in root.findall("./channel/item"):
        attrs = {}
        for attr in item.findall("{http://torznab.com/schemas/2015/feed}attr"):
            name = attr.attrib.get("name")
            value = attr.attrib.get("value")
            if name:
                attrs[name] = value

        yield SearchResult(
            indexer=indexer,
            title=item.findtext("title", default="(untitled)"),
            size=format_size(item.findtext("size")),
            seeders=attrs.get("seeders", "?"),
            leechers=attrs.get("leechers") or attrs.get("peers", "?"),
            category=attrs.get("category", "?"),
            guid=item.findtext("guid", default=""),
            link=item.findtext("link", default=""),
        )


def display_name(indexer: str) -> str:
    return re.sub(r"[_-]+", " ", indexer).title()


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", title.lower())).strip()


def parse_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def download_reference_for_transmission(result: SearchResult, jackett_base_url: str) -> str:
    if result.link.startswith("magnet:"):
        return result.link
    if result.guid.startswith("magnet:"):
        return result.guid
    if result.link:
        return rewrite_url_base(result.link, jackett_base_url)
    return result.guid


def build_torrent_add_arguments(
    result: SearchResult,
    transmission_jackett_base_url: str,
    local_jackett_base_url: str,
    timeout: float,
) -> dict[str, str]:
    reference = download_reference_for_transmission(result, transmission_jackett_base_url)
    if reference.startswith("magnet:"):
        return {"filename": reference}

    local_reference = download_reference_for_transmission(result, local_jackett_base_url)
    redirect_target = get_redirect_location(local_reference, timeout)
    if redirect_target and redirect_target.startswith("magnet:"):
        return {"filename": redirect_target}
    metainfo = fetch_torrent_metainfo(local_reference, timeout)
    return {"metainfo": metainfo}


def fetch_torrent_metainfo(url: str, timeout: float) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "private-torrent-downloader/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return base64.b64encode(payload).decode("ascii")


def get_redirect_location(url: str, timeout: float) -> str | None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "private-torrent-downloader/1.0"},
    )
    opener = urllib.request.build_opener(NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.headers.get("Location")
    except urllib.error.HTTPError as error:
        if error.code in {301, 302, 303, 307, 308}:
            return error.headers.get("Location")
        return None


def rewrite_url_base(url: str, base_url: str) -> str:
    parsed_url = urllib.parse.urlparse(url)
    parsed_base = urllib.parse.urlparse(base_url)
    if not parsed_url.scheme or not parsed_base.scheme:
        return url
    return urllib.parse.urlunparse(
        parsed_url._replace(
            scheme=parsed_base.scheme,
            netloc=parsed_base.netloc,
        )
    )


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TransmissionClient:
    def __init__(self, rpc_url: str, timeout: float = 15):
        self.rpc_url = rpc_url
        self.timeout = timeout
        self.session_id = None

    def request(self, method: str, arguments: dict | None = None) -> dict:
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
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
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

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.session_id:
            headers["X-Transmission-Session-Id"] = self.session_id
        return headers

    def add_torrent(self, arguments: dict[str, str]) -> dict:
        response = self.request("torrent-add", arguments)
        return response.get("torrent-added") or response.get("torrent-duplicate") or {}

    def get_torrents(self) -> list[dict]:
        return self.request("torrent-get", {
            "fields": [
                "id",
                "name",
                "hashString",
                "status",
                "totalSize",
                "percentDone",
                "leftUntilDone",
                "rateDownload",
                "rateUpload",
                "eta",
                "downloadDir",
                "peersConnected",
                "peersGettingFromUs",
                "peersSendingToUs",
            ],
        }).get("torrents", [])

    def start_torrents(self, torrent_ids: list[int]) -> None:
        if torrent_ids:
            self.request("torrent-start", {"ids": torrent_ids})

    def stop_torrents(self, torrent_ids: list[int]) -> None:
        if torrent_ids:
            self.request("torrent-stop", {"ids": torrent_ids})

    def remove_torrents(self, torrent_ids: list[int], delete_local_data: bool = False) -> None:
        if torrent_ids:
            self.request("torrent-remove", {
                "ids": torrent_ids,
                "delete-local-data": delete_local_data,
            })
