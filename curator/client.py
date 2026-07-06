from __future__ import annotations

import base64
import http.cookiejar
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
    size_bytes: int
    seeders: str
    leechers: str
    categories: tuple[str, ...]
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

    @property
    def category(self) -> str:
        return self.categories[0] if self.categories else "?"


@dataclass(frozen=True)
class JackettIndexer:
    id: str
    title: str
    configured: bool
    search_types: tuple[str, ...]
    categories: tuple[str, ...]


def load_api_key(config_dir: Path) -> str:
    jackett_config_file = config_dir / JACKETT_CONFIG_FILE
    data = json.loads(jackett_config_file.read_text(encoding="utf-8"))
    api_key = data.get("APIKey")
    if not api_key:
        raise KeyError(f"APIKey not found in {jackett_config_file}")
    return api_key


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


def build_search_url(
    base_url: str,
    api_key: str,
    indexer: str,
    query: str,
    categories: tuple[str, ...] = (),
) -> str:
    params_dict = {
        "apikey": api_key,
        "t": SEARCH_MODE,
        "q": query,
    }
    if categories:
        params_dict["cat"] = ",".join(categories)
    params = urllib.parse.urlencode(params_dict)
    return f"{base_url.rstrip('/')}/api/v2.0/indexers/{indexer}/results/torznab/api?{params}"


def build_indexers_url(base_url: str, api_key: str, configured: bool | None = None) -> str:
    params_dict = {
        "apikey": api_key,
        "t": "indexers",
    }
    if configured is not None:
        params_dict["configured"] = "true" if configured else "false"
    params = urllib.parse.urlencode(params_dict)
    return f"{base_url.rstrip('/')}/api/v2.0/indexers/all/results/torznab/api?{params}"


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
    categories: tuple[str, ...],
    api_key: str,
    base_url: str,
    timeout: float,
) -> list[SearchResult]:
    url = build_search_url(base_url, api_key, indexer, query, categories)
    try:
        return list(parse_items(fetch_xml(url, timeout), indexer))
    except urllib.error.HTTPError as error:
        if error.code != 400 or not categories:
            raise

    fallback_url = build_search_url(base_url, api_key, indexer, query)
    results = list(parse_items(fetch_xml(fallback_url, timeout), indexer))
    return filter_results_by_categories(results, categories)


def search_indexers(
    query: str,
    indexers: tuple[str, ...],
    categories: tuple[str, ...],
    api_key: str,
    base_url: str,
    timeout: float,
) -> tuple[list[SearchResult], dict[str, str]]:
    results: list[SearchResult] = []
    errors: dict[str, str] = {}
    for indexer in indexers:
        try:
            results.extend(search_indexer(query, indexer, categories, api_key, base_url, timeout))
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            errors[indexer] = summarize_jackett_http_error(error.code, details)
        except Exception as error:
            errors[indexer] = str(error)
    return results, errors


def fetch_indexers(
    api_key: str,
    base_url: str,
    timeout: float,
    configured: bool | None = None,
) -> dict[str, JackettIndexer]:
    payload = fetch_xml(build_indexers_url(base_url, api_key, configured), timeout)
    return {
        indexer.id: indexer
        for indexer in parse_indexers(payload)
    }


def enable_indexer(
    indexer: str,
    base_url: str,
    admin_password: str,
    timeout: float,
) -> None:
    opener = authenticated_jackett_opener(base_url, admin_password, timeout)
    config_url = f"{base_url.rstrip('/')}/api/v2.0/indexers/{indexer}/config"
    config = request_json(opener, config_url, timeout=timeout)
    payload = json.dumps(config).encode("utf-8")
    request = urllib.request.Request(
        config_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "private-torrent-downloader/1.0",
        },
        method="POST",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(summarize_jackett_http_error(error.code, details)) from error
    if not body:
        return
    data = json.loads(body.decode("utf-8"))
    if isinstance(data, dict) and data.get("result") == "error":
        raise RuntimeError(data.get("error") or f"Jackett failed to configure {indexer}")


def summarize_jackett_http_error(status_code: int, body: str) -> str:
    xml_match = re.search(
        r"<error[^>]*description=\"([^\"]+)\"",
        body,
        flags=re.IGNORECASE,
    )
    if xml_match:
        return f"Jackett HTTP {status_code}: {xml_match.group(1).strip()}"
    if "UnauthorizedAccessException" in body or "Permission denied" in body:
        return f"Jackett HTTP {status_code}: permission denied writing indexer config"
    match = re.search(r"<title>([^<]+)</title>", body, flags=re.IGNORECASE)
    if match:
        return f"Jackett HTTP {status_code}: {html_to_text(match.group(1))}"
    text = html_to_text(body).strip()
    if text:
        return f"Jackett HTTP {status_code}: {text[:240]}"
    return f"Jackett HTTP {status_code}"


def html_to_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def authenticated_jackett_opener(
    base_url: str,
    admin_password: str,
    timeout: float,
) -> urllib.request.OpenerDirector:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    login_url = f"{base_url.rstrip('/')}/UI/Dashboard"
    payload = urllib.parse.urlencode({"password": admin_password}).encode("utf-8")
    request = urllib.request.Request(
        login_url,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "private-torrent-downloader/1.0",
        },
        method="POST",
    )
    opener.open(request, timeout=timeout).read()
    return opener


def request_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    timeout: float,
):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "private-torrent-downloader/1.0"},
    )
    with opener.open(request, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8"))


def parse_indexers(payload: bytes) -> list[JackettIndexer]:
    root = ET.fromstring(payload)
    if root.tag == "error":
        code = root.attrib.get("code", "?")
        description = root.attrib.get("description", "unknown error")
        raise RuntimeError(f"Jackett error {code}: {description}")

    parsed: list[JackettIndexer] = []
    for indexer in root.findall("./indexer"):
        caps = indexer.find("caps")
        search_types: list[str] = []
        categories: list[str] = []
        if caps is not None:
            searching = caps.find("searching")
            if searching is not None:
                for child in list(searching):
                    if child.attrib.get("available") == "yes":
                        search_types.append(child.tag)
            categories_node = caps.find("categories")
            if categories_node is not None:
                collect_category_ids(categories_node, categories)

        parsed.append(JackettIndexer(
            id=indexer.attrib.get("id", ""),
            title=indexer.findtext("title", default=indexer.attrib.get("id", "")),
            configured=indexer.attrib.get("configured") == "true",
            search_types=tuple(search_types),
            categories=tuple(categories),
        ))
    return parsed


def collect_category_ids(node: ET.Element, categories: list[str]) -> None:
    for child in list(node):
        category_id = child.attrib.get("id")
        if category_id:
            categories.append(category_id)
        collect_category_ids(child, categories)


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
    if root.tag == "error":
        code = root.attrib.get("code", "?")
        description = root.attrib.get("description", "unknown error")
        raise RuntimeError(f"Jackett error {code}: {description}")
    for item in root.findall("./channel/item"):
        attrs = {}
        category_values: list[str] = []
        for attr in item.findall("{http://torznab.com/schemas/2015/feed}attr"):
            name = attr.attrib.get("name")
            value = attr.attrib.get("value")
            if name == "category" and value:
                category_values.append(value)
            if name:
                attrs[name] = value

        yield SearchResult(
            indexer=indexer,
            title=item.findtext("title", default="(untitled)"),
            size=format_size(item.findtext("size")),
            size_bytes=parse_int(item.findtext("size")),
            seeders=attrs.get("seeders", "?"),
            leechers=attrs.get("leechers") or attrs.get("peers", "?"),
            categories=tuple(category_values),
            guid=item.findtext("guid", default=""),
            link=item.findtext("link", default=""),
        )


def filter_results_by_categories(
    results: list[SearchResult],
    selected_categories: tuple[str, ...],
) -> list[SearchResult]:
    if not selected_categories:
        return results
    return [
        result
        for result in results
        if any(category_matches(selected, actual) for selected in selected_categories for actual in result.categories)
    ]


def category_matches(selected: str, actual: str) -> bool:
    try:
        selected_value = int(selected)
        actual_value = int(actual)
    except (TypeError, ValueError):
        return str(selected) == str(actual)

    if selected_value % 1000 == 0:
        return selected_value // 1000 == actual_value // 1000
    return selected_value == actual_value


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
                "error",
                "errorString",
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
