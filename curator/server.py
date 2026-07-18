from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime
from datetime import timezone
import errno
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from flask import Flask, jsonify, redirect, render_template, request, url_for

from .client import (
    JackettIndexer,
    SearchResult,
    TransmissionClient,
    build_torrent_add_arguments,
    display_name,
    enable_indexer,
    fetch_indexers,
    format_percent_done,
    format_size,
    load_api_key,
    search_indexers,
)
from .config import CuratorConfig, MediaTypeConfig, load_config
from .core import (
    desired_indexers_by_media,
    infer_media_key,
    indexer_reconciliation_failure_count,
    indexer_reconciliation_report,
    suggest_move_target,
    usable_indexers,
)


TORRENT_STATUS = {
    0: "paused",
    1: "check wait",
    2: "checking",
    3: "download wait",
    4: "downloading",
    5: "seed wait",
    6: "seeding",
}

CURATOR_MEDIA_LABEL_PREFIX = "curator:"
ACTIVITY_LIMIT = 20
LOGGER = logging.getLogger("curator")


@dataclass
class BackgroundJob:
    id: str
    label: str
    status: str = "running"
    message: str = ""
    error: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class JobStore:
    def __init__(self):
        self.jobs: dict[str, BackgroundJob] = {}
        self.lock = threading.Lock()

    def start(self, label: str, target, *args) -> str:
        job_id = uuid4().hex
        with self.lock:
            self.jobs[job_id] = BackgroundJob(id=job_id, label=label, message=f"{label} started.")
            self._prune_locked()
        LOGGER.info("%s started", label)
        thread = threading.Thread(target=self._run, args=(job_id, target, args), daemon=True)
        thread.start()
        return job_id

    def record(self, message: str, status: str = "complete") -> str:
        activity_id = uuid4().hex
        activity = BackgroundJob(
            id=activity_id,
            label=message,
            status=status,
            message=message,
            error=message if status == "failed" else "",
        )
        with self.lock:
            self.jobs[activity_id] = activity
            self._prune_locked()
        log = LOGGER.error
        if status == "warning":
            log = LOGGER.warning
        elif status != "failed":
            log = LOGGER.info
        log("%s", message)
        return activity_id

    def _run(self, job_id: str, target, args) -> None:
        try:
            message = target(*args)
        except Exception as error:
            with self.lock:
                job = self.jobs[job_id]
                job.status = "failed"
                job.error = f"{job.label} failed: {error}"
                job.message = f"{job.label} failed."
                job.updated_at = datetime.now(timezone.utc)
                self._prune_locked()
            LOGGER.error("%s", job.error)
            return
        with self.lock:
            job = self.jobs[job_id]
            job.status = "complete"
            job.message = str(message)
            job.updated_at = datetime.now(timezone.utc)
            self._prune_locked()
        LOGGER.info("%s", job.message)

    def recent(self, limit: int = 6) -> list[BackgroundJob]:
        with self.lock:
            running = sorted(
                (job for job in self.jobs.values() if job.status == "running"),
                key=lambda job: job.created_at,
                reverse=True,
            )
            completed = sorted(
                (job for job in self.jobs.values() if job.status != "running"),
                key=lambda job: job.updated_at,
                reverse=True,
            )
            return running + completed[:max(0, limit - len(running))]

    def _prune_locked(self) -> None:
        if len(self.jobs) <= ACTIVITY_LIMIT:
            return
        completed = sorted(
            (job for job in self.jobs.values() if job.status != "running"),
            key=lambda job: job.updated_at,
        )
        while len(self.jobs) > ACTIVITY_LIMIT and completed:
            del self.jobs[completed.pop(0).id]


class CuratorService:
    def __init__(self, config: CuratorConfig):
        self.config = config
        self.api_key: str | None = None
        self.jackett_indexers: dict[str, JackettIndexer] = {}
        self.indexer_report_lines: list[str] = []
        self.indexer_failure_count = 0
        self.indexer_enabled_count = 0
        self.indexer_checking = False
        self.indexer_error = ""

    def jackett_api_key(self) -> str:
        if self.api_key is None:
            self.api_key = load_api_key(self.config.jackett_config_dir)
        return self.api_key

    def config_snapshot(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config.config_path),
            "downloads_root": str(self.config.downloads_root),
            "library_root": str(self.config.library_root),
            "jackett_url": self.config.jackett_base_url,
            "transmission_url": self.config.transmission_rpc_url,
            "transmission_web_url": self.config.transmission_web_url,
            "gluetun_state_path": str(self.config.gluetun_state_path),
            "timeout": self.config.timeout,
            "default_media_type": self.config.default_media_type,
            "default_sort": self.config.default_sort,
            "media_types": self.config.media_types,
        }

    def reconcile_indexers(self) -> None:
        self.indexer_checking = True
        self.indexer_error = ""
        desired = desired_indexers_by_media(self.config.media_types)
        try:
            catalog = fetch_indexers(self.jackett_api_key(), self.config.jackett_base_url, self.config.timeout)
            catalog, enabled, failed = self.enable_missing_indexers(tuple(desired), catalog)
            self.jackett_indexers = catalog
            self.indexer_enabled_count = len(enabled)
            self.indexer_failure_count = indexer_reconciliation_failure_count(desired, catalog, failed)
            self.indexer_report_lines = indexer_reconciliation_report(desired, catalog, enabled, failed)
        except Exception as error:
            self.indexer_error = str(error)
            raise
        finally:
            self.indexer_checking = False

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

    def search(self, media: MediaTypeConfig, query: str) -> tuple[list[SearchResult], dict[str, str]]:
        catalog = self.jackett_indexers
        if not catalog:
            catalog = fetch_indexers(self.jackett_api_key(), self.config.jackett_base_url, self.config.timeout)
            self.jackett_indexers = catalog
        active_indexers, config_errors = usable_indexers(media.indexers, media.categories, catalog)
        if not active_indexers:
            return [], config_errors

        results, errors = search_indexers(
            query,
            active_indexers,
            media.categories,
            self.jackett_api_key(),
            self.config.jackett_base_url,
            self.config.timeout,
        )
        return results, config_errors | errors

    def add_download(self, result: SearchResult, media_key: str) -> dict[str, Any]:
        arguments = build_torrent_add_arguments(
            result,
            self.config.transmission_jackett_base_url,
            self.config.jackett_base_url,
            self.config.timeout,
        )
        arguments["labels"] = [f"{CURATOR_MEDIA_LABEL_PREFIX}{media_key}"]
        return TransmissionClient(self.config.transmission_rpc_url).add_torrent(arguments)

    def torrents(self) -> list[dict]:
        return TransmissionClient(self.config.transmission_rpc_url).get_torrents()

    def torrent(self, torrent_id: int) -> dict:
        for torrent in self.torrents():
            if torrent.get("id") == torrent_id:
                return torrent
        raise KeyError(f"torrent {torrent_id} not found")

    def control_torrent(self, action: str, torrent_id: int) -> None:
        client = TransmissionClient(self.config.transmission_rpc_url)
        if action == "start":
            client.start_torrents([torrent_id])
        elif action == "stop":
            client.stop_torrents([torrent_id])
        elif action == "remove_destroy":
            client.remove_torrents([torrent_id], delete_local_data=True)
        else:
            raise ValueError(f"unsupported torrent action: {action}")

    def move_suggestion(self, media: MediaTypeConfig, torrent: dict) -> dict[str, Any]:
        suggestion = suggest_move_target(
            categories=media.categories,
            library_dir=self.local_dest_path(str(media.library_dir)),
            source_name=torrent.get("name") or "",
            source_is_dir=self.torrent_source_is_dir(torrent),
        )
        return asdict(suggestion)

    def move_completed_torrent(
        self,
        torrent: dict,
        dest_dir: str,
        filename: str,
        create_dir: bool,
    ) -> str:
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

        try:
            os.rename(source_local, dest_local)
        except OSError as error:
            if error.errno == errno.EXDEV:
                raise RuntimeError(
                    "move refused: downloads and library must be on the same Curator mount"
                ) from error
            detail = error.strerror or str(error)
            raise RuntimeError(f"move failed from {source_local} to {dest_local}: {detail}") from error
        TransmissionClient(self.config.transmission_rpc_url).remove_torrents(
            [torrent.get("id")],
            delete_local_data=False,
        )
        return f"Archived to {dest_dir.rstrip('/')}/{filename} and removed from Transmission."

    def transmission_path_to_local(self, transmission_path: str) -> Path:
        path = PurePosixPath(transmission_path)
        if not path.is_absolute():
            raise RuntimeError(f"Transmission path is not absolute: {transmission_path}")
        if not path.parts or path.parts[1] != "downloads":
            raise RuntimeError(f"Only /downloads paths are supported: {transmission_path}")
        return self.config.downloads_root.joinpath(*path.parts[2:])

    def local_dest_path(self, path_text: str) -> Path:
        path = Path(path_text)
        if path.is_absolute():
            return path
        return self.config.library_root / path

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

    def indexer_summary(self) -> dict[str, Any]:
        if self.indexer_checking:
            return {"state": "checking", "message": "Checking configured Jackett indexers...", "details": []}
        if self.indexer_error and not self.jackett_indexers:
            return {"state": "warning", "message": f"Indexer check failed: {self.indexer_error}", "details": []}
        if not self.jackett_indexers:
            return {"state": "unchecked", "message": "Indexers have not been checked yet.", "details": []}
        if self.indexer_failure_count:
            return {
                "state": "warning",
                "message": f"Indexer check found {self.indexer_failure_count} issue(s).",
                "details": self.indexer_report_lines,
            }
        if self.indexer_enabled_count:
            return {
                "state": "ready",
                "message": f"Indexers ready. Enabled {self.indexer_enabled_count}.",
                "details": self.indexer_report_lines,
            }
        return {"state": "ready", "message": "Indexers ready.", "details": self.indexer_report_lines}


def create_app(config: CuratorConfig) -> Flask:
    app = Flask(__name__)
    app.jinja_env.filters["display_name"] = display_name
    app.jinja_env.filters["torrent_view"] = torrent_view
    service = CuratorService(config)
    jobs = JobStore()
    start_indexer_check(service)
    start_config_watch(service)

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            **dashboard_context(
                service,
                jobs,
                selected_media_key(service.config, request.args.get("media_key")),
                query=request.args.get("query", ""),
                results=[],
            ),
        )

    @app.post("/search")
    def search():
        media_key = selected_media_key(service.config, request.form.get("media_key"))
        query = str(request.form.get("query") or "").strip()
        if not query:
            jobs.record("Search failed: enter a search term.", status="failed")
            return render_template(
                "index.html",
                **dashboard_context(
                    service,
                    jobs,
                    media_key,
                    query="",
                    results=[],
                ),
            )
        try:
            results, errors = service.search(service.config.media_types[media_key], query)
            message = f"Search for {query!r} found {len(results)} result(s)."
            if errors:
                details = "; ".join(f"{display_name(indexer)}: {detail}" for indexer, detail in errors.items())
                message = f"Search for {query!r} found {len(results)} result(s). Indexer issues: {details}"
        except Exception as error:
            results = []
            errors = {}
            jobs.record(f"Search for {query!r} failed: {error}", status="failed")
        else:
            jobs.record(message, status="warning" if errors else "complete")
        return render_template(
            "index.html",
            **dashboard_context(
                service,
                jobs,
                media_key,
                query=query,
                results=results,
            ),
        )

    @app.post("/downloads/add")
    def add_download():
        media_key = selected_media_key(service.config, request.form.get("media_key"))
        query = str(request.form.get("query") or "").strip()
        result = search_result_from_form(request.form)
        try:
            torrent = service.add_download(result, media_key)
        except Exception as error:
            jobs.record(f"Add {result.title} failed: {error}", status="failed")
            return render_dashboard_with_optional_search(
                service,
                jobs,
                media_key,
                query,
            )
        name = torrent.get("name") or result.title
        jobs.record(f"Added to Transmission: {name}")
        return render_dashboard_with_optional_search(
            service,
            jobs,
            media_key,
            query,
        )

    @app.post("/torrents/<int:torrent_id>/<action>")
    def control_torrent(torrent_id: int, action: str):
        media_key = selected_media_key(service.config, request.form.get("media_key"))
        query = str(request.form.get("query") or "").strip()
        wants_json = "application/json" in request.headers.get("Accept", "")
        torrent_name = f"torrent {torrent_id}"
        try:
            torrent_name = service.torrent(torrent_id).get("name") or torrent_name
            service.control_torrent(action, torrent_id)
        except Exception as error:
            message = f"Update {torrent_name} failed: {error}"
            jobs.record(message, status="failed")
            if wants_json:
                return jsonify({"error": message}), 500
            return render_dashboard_with_optional_search(
                service,
                jobs,
                media_key,
                query,
            )
        messages = {
            "start": f"Resumed {torrent_name}.",
            "stop": f"Paused {torrent_name}.",
            "remove_destroy": f"Removed {torrent_name} and deleted its local data.",
        }
        message = messages.get(action, f"Updated {torrent_name}.")
        jobs.record(message)
        if wants_json:
            return jsonify({"status": message})
        return render_dashboard_with_optional_search(
            service,
            jobs,
            media_key,
            query,
        )

    @app.get("/torrents/<int:torrent_id>/move")
    def move_form(torrent_id: int):
        media_key = selected_media_key(service.config, request.args.get("media_key"))
        try:
            torrent = service.torrent(torrent_id)
            ensure_complete(torrent)
            media_key = move_media_key(service.config, torrent, media_key)
            suggestion = service.move_suggestion(service.config.media_types[media_key], torrent)
            dest_exists = service.local_dest_path(suggestion["dest_dir"]).exists()
        except Exception as error:
            jobs.record(f"Move setup failed: {error}", status="failed")
            return redirect(url_for("index", media_key=media_key))
        return render_template(
            "move.html",
            config=service.config,
            media_key=media_key,
            media=service.config.media_types[media_key],
            torrent=torrent,
            suggestion=suggestion,
            dest_exists=dest_exists,
            error="",
        )

    @app.post("/torrents/<int:torrent_id>/move")
    def move_torrent(torrent_id: int):
        media_key = selected_media_key(service.config, request.form.get("media_key"))
        dest_dir = str(request.form.get("dest_dir") or "").strip()
        filename = str(request.form.get("filename") or "").strip()
        create_dir = request.form.get("create_dir") == "on"
        try:
            torrent = service.torrent(torrent_id)
            ensure_complete(torrent)
            media_key = move_media_key(service.config, torrent, media_key)
            if not dest_dir or not filename:
                raise ValueError("destination directory and filename are required")
            dest_exists = service.local_dest_path(dest_dir).exists()
            if not dest_exists and not create_dir:
                suggestion = {"dest_dir": dest_dir, "filename": filename, "message": "Destination directory does not exist."}
                return render_template(
                    "move.html",
                    config=service.config,
                    media_key=media_key,
                    media=service.config.media_types[media_key],
                    torrent=torrent,
                    suggestion=suggestion,
                    dest_exists=False,
                    error="Check create directory to archive to this destination.",
                )
        except Exception as error:
            jobs.record(f"Move setup failed: {error}", status="failed")
            return redirect(url_for("index", media_key=media_key))

        jobs.start(
            f"Move {torrent.get('name') or torrent_id}",
            service.move_completed_torrent,
            torrent,
            dest_dir,
            filename,
            create_dir,
        )
        return redirect(url_for("index", media_key=media_key))

    @app.get("/config")
    def config_page():
        media_key = selected_media_key(service.config, request.args.get("media_key"))
        return render_template(
            "config.html",
            config=service.config,
            media_key=media_key,
            media_types=service.config.media_types,
            page_class="config-page",
        )

    @app.get("/api/torrents")
    def api_torrents():
        try:
            torrents = service.torrents()
        except Exception as error:
            return jsonify({"error": str(error)}), 500
        return jsonify({"torrents": [torrent_view(torrent) for torrent in torrents]})

    @app.get("/api/activity")
    def api_activity():
        return jsonify({"activities": [activity_payload(job) for job in jobs.recent()]})

    return app


def activity_payload(job: BackgroundJob) -> dict[str, Any]:
    payload = asdict(job)
    payload["created_at"] = job.created_at.isoformat()
    payload["updated_at"] = job.updated_at.isoformat()
    return payload


def base_context(service: CuratorService, media_key: str) -> dict[str, Any]:
    try:
        torrents = service.torrents()
        torrent_error = ""
    except Exception as error:
        torrents = []
        torrent_error = f"Transmission progress failed: {error}"
    return {
        "config": service.config,
        "transmission_web_url": public_transmission_web_url(service.config.transmission_web_url),
        "media_key": media_key,
        "media": service.config.media_types[media_key],
        "media_types": service.config.media_types,
        "torrents": torrents,
        "torrent_error": torrent_error,
        "indexer_summary": service.indexer_summary(),
    }


def dashboard_context(
    service: CuratorService,
    jobs: JobStore,
    media_key: str,
    query: str,
    results: list[SearchResult],
) -> dict[str, Any]:
    context = base_context(service, media_key)
    context.update(
        {
            "query": query,
            "results": results,
            "overview": system_overview(service, context["torrents"], context["torrent_error"]),
            "activities": jobs.recent(),
            "page_class": "dashboard-page",
        }
    )
    return context


def public_transmission_web_url(configured_url: str) -> str:
    parsed = urlsplit(configured_url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return configured_url
    request_host = request.host.split(":", 1)[0]
    netloc = request_host
    if parsed.port:
        netloc = f"{request_host}:{parsed.port}"
    return urlunsplit((parsed.scheme or request.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def render_dashboard_with_optional_search(
    service: CuratorService,
    jobs: JobStore,
    media_key: str,
    query: str,
):
    results: list[SearchResult] = []
    if query:
        try:
            results, _ = service.search(service.config.media_types[media_key], query)
        except Exception as search_error:
            jobs.record(f"Search refresh for {query!r} failed: {search_error}", status="failed")
    return render_template(
        "index.html",
        **dashboard_context(
            service,
            jobs,
            media_key,
            query=query,
            results=results,
        ),
    )


def start_indexer_check(service: CuratorService) -> None:
    thread = threading.Thread(target=run_startup_indexer_check, args=(service,), daemon=True)
    thread.start()


def run_startup_indexer_check(service: CuratorService) -> None:
    delays = (2, 5, 10, 20, 30, 60)
    for attempt in range(len(delays) + 1):
        try:
            service.reconcile_indexers()
            if attempt:
                LOGGER.info("Indexer check succeeded after retry")
            return
        except Exception as error:
            LOGGER.error("Indexer check failed: %s", error)
            if attempt == len(delays):
                return
            time.sleep(delays[attempt])


def start_config_watch(service: CuratorService) -> None:
    thread = threading.Thread(target=watch_config_file, args=(service,), daemon=True)
    thread.start()


def watch_config_file(service: CuratorService, interval: int = 10) -> None:
    config_path = service.config.config_path
    current_digest = file_digest(config_path)
    while True:
        time.sleep(interval)
        next_digest = file_digest(config_path)
        if not next_digest or next_digest == current_digest:
            continue
        try:
            next_config = load_config(config_path)
        except Exception as error:
            service.indexer_error = f"Config reload failed: {error}"
            LOGGER.error("Config reload failed: %s", error)
            continue
        current_digest = next_digest
        service.config = next_config
        service.api_key = None
        service.jackett_indexers = {}
        LOGGER.info("Config reloaded from %s", config_path)
        try:
            service.reconcile_indexers()
        except Exception as error:
            LOGGER.error("Indexer check failed after config reload: %s", error)


def file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return ""


def selected_media_key(config: CuratorConfig, value: str | None) -> str:
    if value in config.media_types:
        return str(value)
    return config.default_media_type


def move_media_key(config: CuratorConfig, torrent: dict, fallback_key: str) -> str:
    for label in torrent.get("labels") or ():
        if not isinstance(label, str) or not label.startswith(CURATOR_MEDIA_LABEL_PREFIX):
            continue
        media_key = label.removeprefix(CURATOR_MEDIA_LABEL_PREFIX)
        if media_key in config.media_types:
            return media_key

    inferred_key = infer_media_key(str(torrent.get("name") or ""), config.media_types)
    if inferred_key is not None:
        return inferred_key
    return fallback_key


def ensure_complete(torrent: dict) -> None:
    if torrent_is_complete(torrent):
        return
    raise RuntimeError("selected torrent is not complete")


def search_result_from_form(form) -> SearchResult:
    return SearchResult(
        indexer=str(form["indexer"]),
        title=str(form["title"]),
        size=str(form.get("size") or "?"),
        size_bytes=int(form.get("size_bytes") or 0),
        seeders=str(form.get("seeders") or "?"),
        leechers=str(form.get("leechers") or "?"),
        categories=tuple(item for item in str(form.get("categories") or "").split(",") if item),
        guid=str(form.get("guid") or ""),
        link=str(form.get("link") or ""),
    )


def torrent_view(torrent: dict) -> dict[str, Any]:
    return {
        "id": torrent.get("id"),
        "name": torrent.get("name") or "",
        "state": TORRENT_STATUS.get(torrent.get("status"), str(torrent.get("status"))),
        "size": format_size(torrent.get("totalSize")),
        "progress": format_percent(torrent.get("percentDone")),
        "eta": format_eta(torrent.get("eta")),
        "download_rate": format_rate(torrent.get("rateDownload")),
        "peers": format_peers(torrent),
        "complete": torrent_is_complete(torrent),
        "paused": torrent.get("status") == 0,
    }


def torrent_is_complete(torrent: dict) -> bool:
    percent_done = torrent.get("percentDone")
    if isinstance(percent_done, (int, float)):
        return percent_done >= 1
    total_size = torrent.get("totalSize")
    return torrent.get("leftUntilDone") == 0 and isinstance(total_size, int) and total_size > 0


def format_percent(value) -> str:
    if isinstance(value, (int, float)):
        return format_percent_done(float(value))
    return "?"


def format_eta(value) -> str:
    if not isinstance(value, int) or value < 0:
        return "?"
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def format_rate(value) -> str:
    if not isinstance(value, int):
        return "?"
    if value <= 0:
        return "0"
    size = float(value)
    for unit in ("B/s", "KiB/s", "MiB/s", "GiB/s"):
        if size < 1024 or unit == "GiB/s":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "?"


def format_peers(torrent: dict) -> str:
    active = torrent.get("peersSendingToUs")
    connected = torrent.get("peersConnected")
    if isinstance(active, int) and isinstance(connected, int):
        return f"{active}/{connected}"
    if isinstance(connected, int):
        return str(connected)
    return "?"


def system_overview(service: CuratorService, torrents: list[dict], torrent_error: str = "") -> dict[str, Any]:
    return {
        "downloads": disk_usage_view("Downloads", service.config.downloads_root),
        "library": disk_usage_view("Library", service.config.library_root),
        "jackett": jackett_status(service),
        "transmission": transmission_status(torrents, torrent_error),
        "gluetun": gluetun_status(service.config.gluetun_state_path),
    }


def disk_usage_view(label: str, path: Path) -> dict[str, str]:
    try:
        usage = shutil.disk_usage(path)
    except Exception as error:
        return {"label": label, "path": str(path), "state": "warning", "summary": f"Unavailable: {error}"}
    used = usage.total - usage.free
    percent = int((used / usage.total) * 100) if usage.total else 0
    return {
        "label": label,
        "path": str(path),
        "state": "ready" if percent < 90 else "warning",
        "summary": f"{format_size(usage.free)} free of {format_size(usage.total)}",
        "percent": f"{percent}%",
    }


def jackett_status(service: CuratorService) -> dict[str, str]:
    summary = service.indexer_summary()
    return {"state": summary["state"], "summary": summary["message"]}


def transmission_status(torrents: list[dict], torrent_error: str = "") -> dict[str, str]:
    if torrent_error:
        return {"state": "warning", "summary": torrent_error}
    active = sum(1 for torrent in torrents if torrent.get("status") == 4)
    return {"state": "ready", "summary": f"{len(torrents)} torrent(s), {active} downloading"}


def gluetun_status(state_path: Path) -> dict[str, str]:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"state": "warning", "summary": f"VPN state file not found: {state_path}"}
    except Exception as error:
        return {"state": "warning", "summary": f"VPN state unavailable: {error}"}

    location = data.get("country_short") or data.get("country_long") or "unknown"
    host = data.get("host_name") or data.get("ip") or "unknown host"
    generated_at = data.get("generated_at") or "unknown refresh time"
    return {
        "state": "ready",
        "summary": f"{location} via {host}",
        "detail": f"Last VPNGate refresh: {humanize_timestamp(generated_at)}",
    }


def humanize_timestamp(value: str) -> str:
    try:
        timestamp = datetime.fromisoformat(value)
    except Exception:
        return str(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - timestamp.astimezone(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Curator web server.")
    parser.add_argument("--config", type=Path, default=Path("curator-config/curator.toml"))
    parser.add_argument("--host", default=os.environ.get("CURATOR_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CURATOR_PORT", "8787")))
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app = create_app(load_config(args.config))
    LOGGER.info("Web listening on http://%s:%s", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
