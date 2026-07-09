from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from typing import Any

from .client import SearchResult
from .config import MediaTypeConfig
from .core import MoveSuggestion


class CuratorRemoteClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        request_timeout: float = 30,
        search_timeout: float = 20,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.request_timeout = request_timeout
        self.search_timeout = search_timeout

    def reconcile_indexers(self, media_types: dict[str, MediaTypeConfig]) -> dict[str, Any]:
        return self.post(
            "/api/indexers/reconcile",
            {
                "media_types": media_types_payload(media_types),
                "media_keys": list(media_types),
                "timeout": self.search_timeout,
            },
        )

    def search(self, media: MediaTypeConfig, query: str) -> tuple[list[SearchResult], dict[str, str]]:
        data = self.post(
            "/api/search",
            {
                "media": media_payload(media),
                "media_key": media.key,
                "query": query,
                "timeout": self.search_timeout,
            },
        )
        return [search_result_from_dict(item) for item in data.get("results", [])], dict(data.get("errors", {}))

    def torrents(self) -> list[dict]:
        return list(self.get("/api/torrents").get("torrents", []))

    def add_download(self, result: SearchResult) -> dict:
        return dict(self.post("/api/downloads/add", {"result": asdict(result)}).get("torrent", {}))

    def control_torrent(self, action: str, torrent_id: int | None) -> list[dict]:
        data = self.post("/api/torrents/control", {"action": action, "torrent_id": torrent_id})
        return list(data.get("torrents", []))

    def move_suggestion(self, media: MediaTypeConfig, torrent: dict) -> MoveSuggestion:
        data = self.post(
            "/api/move/suggest",
            {
                "media": media_payload(media),
                "media_key": media.key,
                "torrent": torrent,
            },
        )
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
            timeout=max(self.request_timeout, 300.0),
        )

    def get(self, path: str) -> dict[str, Any]:
        return self.request("GET", path)

    def post(
        self,
        path: str,
        payload: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self.request("POST", path, payload, timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
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
            with urllib.request.urlopen(request, timeout=timeout or self.request_timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            try:
                details = json.loads(body)
            except json.JSONDecodeError:
                details = {"error": body or error.reason}
            message = str(details.get("error") or error.reason)
            if "missing field: 'media_key'" in message:
                message = (
                    "Curator server expects the older API shape. Rebuild/restart the server, "
                    "or point the client at the updated server."
                )
            raise RuntimeError(message) from error
        return json.loads(body.decode("utf-8"))


def media_types_payload(media_types: dict[str, MediaTypeConfig]) -> dict[str, dict[str, Any]]:
    return {
        key: media_payload(media)
        for key, media in media_types.items()
    }


def media_payload(media: MediaTypeConfig) -> dict[str, Any]:
    return {
        "key": media.key,
        "label": media.label,
        "indexers": list(media.indexers),
        "categories": list(media.categories),
        "library_dir": str(media.library_dir),
    }


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
