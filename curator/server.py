from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
import shutil
from typing import Any

from .client import (
    JackettIndexer,
    SearchResult,
    TransmissionClient,
    build_torrent_add_arguments,
    enable_indexer,
    fetch_indexers,
    load_api_key,
    search_indexers,
)
from .config import AppConfig, load_config
from .core import (
    desired_indexers_by_media,
    indexer_reconciliation_failure_count,
    indexer_reconciliation_report,
    suggest_move_target,
    usable_indexers,
)


class CuratorService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.api_key: str | None = None
        self.jackett_indexers: dict[str, JackettIndexer] = {}

    def jackett_api_key(self) -> str:
        if self.api_key is None:
            self.api_key = load_api_key(self.config.jackett_config_dir)
        return self.api_key

    def config_snapshot(self) -> dict[str, Any]:
        return {
            "downloads_dir": str(self.config.downloads_dir),
            "transmission_url": self.config.transmission_rpc_url,
            "timeout": self.config.timeout,
            "max_results": self.config.max_results,
            "default_sort": self.config.default_sort,
            "media_type": self.config.media_type,
            "media_types": {
                key: {
                    "key": media.key,
                    "label": media.label,
                    "indexers": list(media.indexers),
                    "categories": list(media.categories),
                    "library_dir": str(media.library_dir),
                }
                for key, media in self.config.media_types.items()
            },
        }

    def reconcile_indexers(self) -> dict[str, Any]:
        desired = desired_indexers_by_media(self.config.media_types)
        api_key = self.jackett_api_key()
        catalog = fetch_indexers(api_key, self.config.jackett_base_url, self.config.timeout)
        catalog, enabled, failed = self.enable_missing_indexers(tuple(desired), catalog)
        self.jackett_indexers = catalog
        report_lines = indexer_reconciliation_report(desired, catalog, enabled, failed)
        return {
            "catalog": {key: asdict(value) for key, value in catalog.items()},
            "report_lines": report_lines,
            "enabled_count": len(enabled),
            "failure_count": indexer_reconciliation_failure_count(desired, catalog, failed),
        }

    def enable_missing_indexers(
        self,
        indexers: tuple[str, ...],
        catalog: dict[str, JackettIndexer],
    ) -> tuple[dict[str, JackettIndexer], list[str], dict[str, str]]:
        missing = [
            indexer
            for indexer in indexers
            if indexer in catalog and not catalog[indexer].configured
        ]
        if not missing:
            return catalog, [], {}

        enabled: list[str] = []
        errors: dict[str, str] = {}
        for indexer in missing:
            try:
                enable_indexer(
                    indexer,
                    self.config.jackett_base_url,
                    self.config.jackett_admin_password,
                    self.config.timeout,
                )
            except Exception as error:
                errors[indexer] = str(error)
            else:
                enabled.append(indexer)

        try:
            catalog = fetch_indexers(self.jackett_api_key(), self.config.jackett_base_url, self.config.timeout)
        except Exception as error:
            errors["jackett"] = f"could not refresh indexer list: {error}"
            return catalog, enabled, errors

        return catalog, enabled, errors

    def search(self, media_key: str, query: str) -> dict[str, Any]:
        media = self.config.media_types[media_key]
        api_key = self.jackett_api_key()
        catalog = self.jackett_indexers or fetch_indexers(
            api_key,
            self.config.jackett_base_url,
            self.config.timeout,
        )
        self.jackett_indexers = catalog
        active_indexers, config_errors = usable_indexers(media.indexers, media.categories, catalog)
        if not active_indexers:
            return {"results": [], "errors": config_errors}

        results, errors = search_indexers(
            query,
            active_indexers,
            media.categories,
            api_key,
            self.config.jackett_base_url,
            self.config.timeout,
        )
        return {
            "results": [asdict(result) for result in results],
            "errors": config_errors | errors,
        }

    def add_download(self, result: SearchResult) -> dict[str, Any]:
        arguments = build_torrent_add_arguments(
            result,
            self.config.transmission_jackett_base_url,
            self.config.jackett_base_url,
            self.config.timeout,
        )
        torrent = TransmissionClient(self.config.transmission_rpc_url).add_torrent(arguments)
        return {"torrent": torrent}

    def torrents(self) -> dict[str, Any]:
        torrents = TransmissionClient(self.config.transmission_rpc_url).get_torrents()
        return {"torrents": torrents}

    def control_torrent(self, action: str, torrent_id: int | None) -> dict[str, Any]:
        if torrent_id is None:
            raise ValueError("torrent_id is required")
        client = TransmissionClient(self.config.transmission_rpc_url)
        if action == "start":
            client.start_torrents([torrent_id])
        elif action == "stop":
            client.stop_torrents([torrent_id])
        elif action == "remove_destroy":
            client.remove_torrents([torrent_id], delete_local_data=True)
        else:
            raise ValueError(f"unsupported torrent action: {action}")
        return {"torrents": client.get_torrents()}

    def move_suggestion(self, media_key: str, torrent: dict) -> dict[str, Any]:
        media = self.config.media_types[media_key]
        source_name = torrent.get("name") or ""
        suggestion = suggest_move_target(
            media_key=media_key,
            library_dir=media.library_dir,
            source_name=source_name,
            source_is_dir=self.torrent_source_is_dir(torrent),
        )
        return asdict(suggestion)

    def path_exists(self, path_text: str) -> dict[str, Any]:
        return {"exists": self.local_dest_path(path_text).exists()}

    def move_completed_torrent(
        self,
        torrent: dict,
        dest_dir: str,
        filename: str,
        create_dir: bool,
    ) -> dict[str, Any]:
        source_dir = torrent.get("downloadDir") or ""
        source_name = torrent.get("name") or ""
        if not source_dir or not source_name:
            raise RuntimeError("missing torrent source path")

        source_local = self.transmission_path_to_local(source_dir) / source_name
        dest_dir_local = self.local_dest_path(dest_dir)
        dest_local = dest_dir_local / filename

        if create_dir:
            dest_dir_local.mkdir(parents=True, exist_ok=True)
        elif not dest_dir_local.exists():
            raise FileNotFoundError(f"destination directory does not exist: {dest_dir}")

        if not source_local.exists():
            raise FileNotFoundError(f"source path does not exist: {source_local}")
        if source_local == dest_local:
            raise RuntimeError("source and destination are the same")
        if dest_local.exists():
            raise FileExistsError(f"destination already exists: {dest_local}")

        shutil.move(str(source_local), str(dest_local))
        client = TransmissionClient(self.config.transmission_rpc_url)
        client.remove_torrents([torrent.get("id")], delete_local_data=False)
        return {"torrents": client.get_torrents(), "dest_dir": dest_dir, "filename": filename}

    def transmission_path_to_local(self, transmission_path: str) -> Path:
        path = PurePosixPath(transmission_path)
        if not path.is_absolute():
            raise RuntimeError(f"Transmission path is not absolute: {transmission_path}")
        if not path.parts or path.parts[1] != "downloads":
            raise RuntimeError(f"Only /downloads paths are supported: {transmission_path}")
        return self.config.downloads_dir.joinpath(*path.parts[2:])

    def local_dest_path(self, path_text: str) -> Path:
        path = Path(path_text)
        if path.is_absolute():
            return path
        return self.config.config_path.parent / path

    def torrent_source_is_dir(self, torrent: dict) -> bool:
        source_dir = torrent.get("downloadDir") or ""
        source_name = torrent.get("name") or ""
        if not source_dir or not source_name:
            return False
        try:
            source_local = self.transmission_path_to_local(source_dir) / source_name
        except RuntimeError:
            return Path(source_name).suffix == ""
        if source_local.exists():
            return source_local.is_dir()
        return Path(source_name).suffix == ""


class CuratorRequestHandler(BaseHTTPRequestHandler):
    server: CuratorHTTPServer

    def do_GET(self) -> None:
        if not self.authenticate():
            return
        try:
            if self.path == "/api/health":
                self.write_json({"ok": True})
            elif self.path == "/api/config":
                self.write_json(self.server.service.config_snapshot())
            elif self.path == "/api/torrents":
                self.write_json(self.server.service.torrents())
            else:
                self.write_error(HTTPStatus.NOT_FOUND, "not found")
        except Exception as error:
            self.write_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def do_POST(self) -> None:
        if not self.authenticate():
            return
        try:
            payload = self.read_json()
            if self.path == "/api/indexers/reconcile":
                self.write_json(self.server.service.reconcile_indexers())
            elif self.path == "/api/search":
                self.write_json(self.server.service.search(str(payload["media_key"]), str(payload["query"])))
            elif self.path == "/api/downloads/add":
                self.write_json(self.server.service.add_download(search_result_from_dict(payload["result"])))
            elif self.path == "/api/torrents/control":
                self.write_json(
                    self.server.service.control_torrent(
                        str(payload["action"]),
                        payload.get("torrent_id"),
                    )
                )
            elif self.path == "/api/move/suggest":
                self.write_json(
                    self.server.service.move_suggestion(
                        str(payload["media_key"]),
                        payload["torrent"],
                    )
                )
            elif self.path == "/api/path/exists":
                self.write_json(self.server.service.path_exists(str(payload["path"])))
            elif self.path == "/api/torrents/move":
                self.write_json(
                    self.server.service.move_completed_torrent(
                        payload["torrent"],
                        str(payload["dest_dir"]),
                        str(payload["filename"]),
                        bool(payload.get("create_dir")),
                    )
                )
            else:
                self.write_error(HTTPStatus.NOT_FOUND, "not found")
        except KeyError as error:
            self.write_error(HTTPStatus.BAD_REQUEST, f"missing field: {error}")
        except Exception as error:
            self.write_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def authenticate(self) -> bool:
        token = self.server.token
        if not token:
            return True
        expected = f"Bearer {token}"
        if self.headers.get("Authorization") == expected:
            return True
        self.write_error(HTTPStatus.UNAUTHORIZED, "unauthorized")
        return False

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if not length:
            return {}
        payload = self.rfile.read(length)
        return json.loads(payload.decode("utf-8"))

    def write_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_error(self, status: HTTPStatus, message: str) -> None:
        self.write_json({"error": message}, status=status)

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")


class CuratorHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, service: CuratorService, token: str):
        super().__init__(server_address, handler_class)
        self.service = service
        self.token = token


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Curator HTTP server.")
    parser.add_argument("--config", type=Path, default=Path("curator/config.toml"))
    parser.add_argument("--host", default=os.environ.get("CURATOR_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CURATOR_PORT", "8787")))
    parser.add_argument("--token", default=os.environ.get("CURATOR_TOKEN", ""))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    service = CuratorService(load_config(args.config))
    server = CuratorHTTPServer((args.host, args.port), CuratorRequestHandler, service, args.token)
    print(f"Curator server listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
