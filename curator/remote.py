from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .client import JackettIndexer, SearchResult
from .config import AppConfig, MediaTypeConfig
from .core import MoveSuggestion


class CuratorRemoteClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def config(self) -> dict[str, Any]:
        return self.get("/api/config")

    def reconcile_indexers(self) -> dict[str, Any]:
        return self.post("/api/indexers/reconcile", {})

    def search(self, media_key: str, query: str) -> tuple[list[SearchResult], dict[str, str]]:
        data = self.post("/api/search", {"media_key": media_key, "query": query})
        return [search_result_from_dict(item) for item in data.get("results", [])], dict(data.get("errors", {}))

    def torrents(self) -> list[dict]:
        return list(self.get("/api/torrents").get("torrents", []))

    def add_download(self, result: SearchResult) -> dict:
        return dict(self.post("/api/downloads/add", {"result": asdict(result)}).get("torrent", {}))

    def control_torrent(self, action: str, torrent_id: int | None) -> list[dict]:
        data = self.post("/api/torrents/control", {"action": action, "torrent_id": torrent_id})
        return list(data.get("torrents", []))

    def move_suggestion(self, media_key: str, torrent: dict) -> MoveSuggestion:
        data = self.post("/api/move/suggest", {"media_key": media_key, "torrent": torrent})
        return MoveSuggestion(
            dest_dir=str(data.get("dest_dir") or ""),
            filename=str(data.get("filename") or ""),
            message=str(data.get("message") or ""),
        )

    def path_exists(self, path: str) -> bool:
        return bool(self.post("/api/path/exists", {"path": path}).get("exists"))

    def move_completed_torrent(
        self,
        torrent: dict,
        dest_dir: str,
        filename: str,
        create_dir: bool,
    ) -> dict[str, Any]:
        return self.post(
            "/api/torrents/move",
            {
                "torrent": torrent,
                "dest_dir": dest_dir,
                "filename": filename,
                "create_dir": create_dir,
            },
        )

    def get(self, path: str) -> dict[str, Any]:
        return self.request("GET", path)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", path, payload)

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            try:
                details = json.loads(body)
            except json.JSONDecodeError:
                details = {"error": body or error.reason}
            raise RuntimeError(details.get("error") or error.reason) from error
        return json.loads(body.decode("utf-8"))


def app_config_from_remote(snapshot: dict[str, Any], server_url: str) -> AppConfig:
    media_types = {
        key: MediaTypeConfig(
            key=str(value["key"]),
            label=str(value["label"]),
            indexers=tuple(str(item) for item in value.get("indexers", ())),
            categories=tuple(str(item) for item in value.get("categories", ())),
            library_dir=Path(str(value["library_dir"])),
        )
        for key, value in snapshot["media_types"].items()
    }
    media_type = str(snapshot["media_type"])
    return AppConfig(
        config_path=Path("."),
        downloads_dir=Path(str(snapshot.get("downloads_dir") or ".")),
        jackett_base_url=server_url,
        transmission_jackett_base_url=server_url,
        jackett_admin_password="",
        transmission_rpc_url=str(snapshot.get("transmission_url") or server_url),
        jackett_config_dir=Path("."),
        timeout=float(snapshot.get("timeout") or 30),
        max_results=int(snapshot.get("max_results") or 30),
        default_sort=str(snapshot.get("default_sort") or "seeders"),
        media_type=media_type,
        media=media_types[media_type],
        media_types=media_types,
    )


def search_result_from_dict(data: dict[str, Any]) -> SearchResult:
    return SearchResult(
        indexer=str(data["indexer"]),
        title=str(data["title"]),
        size=str(data.get("size") or "?"),
        size_bytes=int(data.get("size_bytes") or 0),
        seeders=str(data.get("seeders") or "?"),
        leechers=str(data.get("leechers") or "?"),
        categories=tuple(str(item) for item in data.get("categories", ())),
        guid=str(data.get("guid") or ""),
        link=str(data.get("link") or ""),
    )
