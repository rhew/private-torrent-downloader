from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import PurePosixPath
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.parse import urlparse

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Select, Static

from .client import (
    build_torrent_add_arguments,
    JackettIndexer,
    SearchResult,
    TransmissionClient,
    display_name,
    enable_indexer,
    fetch_indexers,
    format_size,
    load_api_key,
    search_indexers,
)
from .config import AppConfig, DEFAULT_SORT, load_config
from .core import (
    MoveSuggestion,
    desired_indexers_by_media,
    indexer_reconciliation_failure_count,
    indexer_reconciliation_report,
    suggest_move_target,
    usable_indexers,
)
from .remote import CuratorRemoteClient


RESULT_COLUMNS = (
    ("Seed", "seeders"),
    ("Leech", "leechers"),
    ("Size", "size"),
    ("Indexer", "indexer"),
    ("Title", "title"),
)
SORT_MODES = tuple(key for _, key in RESULT_COLUMNS)
TORRENT_STATUS = {
    0: "paused",
    1: "check wait",
    2: "checking",
    3: "download wait",
    4: "downloading",
    5: "seed wait",
    6: "seeding",
}

HELP_TEXT = """Keys

/ or s   focus search
Esc      focus results
t        focus Transmission
[ or ]   previous/next media type
j/k      move down/up
g/G      first/last row
r        refresh focused pane
a        add selected search result
Space    pause/resume selected torrent
x        remove selected torrent and local data, with confirmation
m        move completed torrent to library
o        cycle result sort
e        show last error details
?        show/hide help
q        quit
"""


@dataclass(frozen=True)
class MoveRequest:
    dest_dir: str
    filename: str


class HelpScreen(ModalScreen):
    CSS = """
    HelpScreen {
        align: center middle;
    }

    #help {
        width: 58;
        height: auto;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("question_mark", "close", "Close", show=False),
        Binding("escape", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static(HELP_TEXT, id="help")

    def action_close(self) -> None:
        self.dismiss()


class NoticeScreen(ModalScreen):
    CSS = """
    NoticeScreen {
        align: center middle;
    }

    #notice {
        width: 62;
        height: auto;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("c", "copy", "Copy", show=False),
        Binding("enter", "close", "Close", show=False),
        Binding("escape", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]

    def __init__(self, message: str, copyable: bool = False):
        super().__init__()
        self.message = message
        self.copyable = copyable

    def compose(self) -> ComposeResult:
        if self.copyable:
            body = f"{self.message}\n\nPress c to copy. Enter/Esc to close."
        else:
            body = f"{self.message}\n\nEnter/Esc to close."
        yield Static(body, id="notice")

    def action_close(self) -> None:
        self.dismiss()

    def action_copy(self) -> None:
        if self.copyable:
            self.app.copy_text_to_clipboard(self.message)


class ConfirmScreen(ModalScreen):
    CSS = """
    ConfirmScreen {
        align: center middle;
    }

    #confirm {
        width: 70;
        height: auto;
        padding: 1 2;
        border: thick $error;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("y", "yes", "Yes", show=False),
        Binding("n", "no", "No", show=False),
        Binding("escape", "no", "No", show=False),
    ]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        yield Static(f"{self.message}\n\nPress y to confirm, n/Esc to cancel.", id="confirm")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class MoveScreen(ModalScreen[MoveRequest | None]):
    CSS = """
    MoveScreen {
        align: center middle;
    }

    #move-dialog {
        width: 76;
        height: auto;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }

    .move-label {
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("enter", "submit", "Move", show=False),
        Binding("tab", "focus_next", "Next", show=False),
        Binding("shift+tab", "focus_previous", "Prev", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("q", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        current_dir: str,
        current_name: str,
        suggestion: MoveSuggestion,
    ):
        super().__init__()
        self.current_dir = current_dir
        self.current_name = current_name
        self.suggestion = suggestion

    def compose(self) -> ComposeResult:
        with Vertical(id="move-dialog"):
            yield Static("Archive completed torrent to a new location and stop tracking it in Transmission.")
            yield Static(f"Current dir: {self.current_dir}")
            yield Static(f"Current name: {self.current_name}")
            yield Static(self.suggestion.message)
            yield Static("Destination dir", classes="move-label")
            yield Input(self.suggestion.dest_dir, id="move-dest-dir")
            yield Static("Final name", classes="move-label")
            yield Input(self.suggestion.filename, id="move-filename")
            yield Static("Enter to move. Tab switches fields. Esc cancels.")

    def on_mount(self) -> None:
        self.query_one("#move-filename", Input).focus()

    def action_submit(self) -> None:
        dest_dir = self.query_one("#move-dest-dir", Input).value.strip()
        filename = self.query_one("#move-filename", Input).value.strip()
        if not dest_dir or not filename:
            return
        self.dismiss(MoveRequest(dest_dir=dest_dir, filename=filename))

    def action_focus_next(self) -> None:
        self.focus_next()

    def action_focus_previous(self) -> None:
        self.focus_previous()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self.action_submit()


class CuratorApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #topbar {
        height: auto;
        margin: 0 1;
    }

    #media-type {
        width: 18;
        margin-right: 1;
    }

    #search {
        width: 1fr;
    }

    #notice {
        height: auto;
        padding: 0 1;
        margin: 0 1;
        border-left: solid $primary;
        color: $text-muted;
        background: $surface;
    }

    #results-title {
        height: 1;
        padding: 0 1;
    }

    #results {
        height: 2fr;
        min-height: 8;
    }

    #results.-stale {
        tint: $warning 15%;
    }

    #transmission-pane {
        height: 1fr;
        border-top: solid $primary;
        min-height: 8;
    }

    #transmission-title {
        height: 1;
        padding: 0 1;
    }

    #transmission {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("/", "focus_search", "Search", show=False),
        Binding("s", "focus_search", "Search", show=False),
        Binding("escape", "focus_results", "Results", show=False),
        Binding("t", "focus_transmission", "Transmission", show=False),
        Binding("[", "previous_media_type", "Prev media", show=False),
        Binding("]", "next_media_type", "Next media", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "cursor_top", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("a", "add_download", "Add", show=False),
        Binding("space", "toggle_torrent_pause", "Pause", show=False),
        Binding("x", "confirm_remove_torrent", "Remove", show=False),
        Binding("m", "move_completed", "Move", show=False),
        Binding("o", "cycle_sort", "Sort", show=False),
        Binding("e", "show_error_details", "Errors", show=False),
        Binding("question_mark", "show_help", "Help"),
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(self, config: AppConfig, remote: CuratorRemoteClient | None = None):
        super().__init__()
        self.config = config
        self.remote = remote
        self.api_key = "" if remote else load_api_key(config.jackett_config_dir)
        self.media_type_keys = list(config.media_types.keys())
        self.current_media_key = config.media_type
        self.results: list[SearchResult] = []
        self.visible_results: list[SearchResult] = []
        self.torrents: list[dict] = []
        self.query = ""
        self.sort_mode = config.default_sort if config.default_sort in SORT_MODES else DEFAULT_SORT
        self.errors: dict[str, str] = {}
        self.jackett_indexers: dict[str, JackettIndexer] = {}
        self.indexer_report_lines: list[str] = []
        self.message: str | None = None
        self.notice_message: str | None = None
        self.notice_kind: str | None = None
        self.search_in_flight = False
        self.search_request_id = 0
        self.pending_remove_torrent_id: int | None = None
        self.pending_move_torrent: dict | None = None
        self.pending_move_request: MoveRequest | None = None

    def compose(self) -> ComposeResult:
        placeholder = f"Search {self.current_media().label.lower()} across configured Jackett indexers"
        with Horizontal(id="topbar"):
            yield Select(
                [(media.label, media.key) for media in self.config.media_types.values()],
                value=self.current_media_key,
                id="media-type",
                allow_blank=False,
            )
            yield Input(placeholder=placeholder, id="search")
        yield Static("", id="notice")
        yield Static("", id="results-title")
        yield DataTable(id="results")
        with Vertical(id="transmission-pane"):
            yield Static("", id="transmission-title")
            yield DataTable(id="transmission")

    def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        self.setup_results_table()
        torrents = self.query_one("#transmission", DataTable)
        torrents.cursor_type = "row"
        torrents.zebra_stripes = True
        torrents.add_columns("State", "Size", "Progress", "ETA", "DL", "Peers", "Name")
        self.query_one("#media-type", Select).focus()
        self.update_notice()
        self.update_title()
        self.update_transmission_title()
        self.set_interval(5, self.refresh_progress)
        self.reconcile_indexers()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            self.query = query
            self.dispatch_search(query)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "media-type":
            return
        if isinstance(event.value, str) and event.value != self.current_media_key:
            self.switch_media_type_to(event.value)

    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self.update_title()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()
        self.update_title()
        self.update_transmission_title()

    def action_focus_results(self) -> None:
        self.query_one("#results", DataTable).focus()
        self.update_title()
        self.update_transmission_title()

    def action_focus_transmission(self) -> None:
        self.query_one("#transmission", DataTable).focus()
        self.update_title()
        self.update_transmission_title()

    def action_previous_media_type(self) -> None:
        self.switch_media_type(-1)

    def action_next_media_type(self) -> None:
        self.switch_media_type(1)

    def action_cursor_down(self) -> None:
        table = self.focused_table()
        table.action_cursor_down()
        table.focus()

    def action_cursor_up(self) -> None:
        table = self.focused_table()
        table.action_cursor_up()
        table.focus()

    def action_cursor_top(self) -> None:
        table = self.focused_table()
        table.move_cursor(row=0, animate=False)
        table.focus()

    def action_cursor_bottom(self) -> None:
        table = self.focused_table()
        row_count = len(self.torrents) if table.id == "transmission" else len(self.visible_results)
        if row_count:
            table.move_cursor(row=row_count - 1, animate=False)
        table.focus()

    def action_refresh(self) -> None:
        if self.focused_table().id == "transmission":
            self.refresh_progress()
        elif self.query:
            self.dispatch_search(self.query)

    def action_add_download(self) -> None:
        result = self.current_result()
        if result:
            self.add_download(result)

    def action_toggle_torrent_pause(self) -> None:
        torrent = self.current_torrent()
        if not torrent:
            return
        if torrent.get("status") == 0:
            self.update_status(f"Resuming {torrent.get('name') or torrent.get('id')}...")
            self.control_torrent("start", torrent.get("id"), torrent.get("name") or "")
        else:
            self.update_status(f"Pausing {torrent.get('name') or torrent.get('id')}...")
            self.control_torrent("stop", torrent.get("id"), torrent.get("name") or "")

    def action_confirm_remove_torrent(self) -> None:
        torrent = self.current_torrent()
        if torrent:
            self.pending_remove_torrent_id = torrent.get("id")
            name = torrent.get("name") or f"torrent {torrent.get('id')}"
            self.push_screen(
                ConfirmScreen(f"Remove {name} from Transmission and destroy local data?"),
                self.remove_torrent_confirmed,
            )

    def remove_torrent_confirmed(self, confirmed) -> None:
        torrent_id = self.pending_remove_torrent_id
        self.pending_remove_torrent_id = None
        if not confirmed:
            return
        if torrent_id is not None:
            self.control_torrent("remove_destroy", torrent_id, "")

    def action_move_completed(self) -> None:
        torrent = self.current_torrent()
        if not torrent:
            return
        if torrent.get("leftUntilDone") != 0 and torrent.get("percentDone") != 1:
            self.push_screen(NoticeScreen("Move skipped: selected torrent is not complete."))
            return
        source_name = torrent.get("name") or ""
        source_dir = torrent.get("downloadDir") or ""
        try:
            if self.remote:
                suggestion = self.remote.move_suggestion(self.current_media(), torrent)
            else:
                suggestion = suggest_move_target(
                    media_key=self.current_media_key,
                    library_dir=self.local_dest_path(str(self.current_media().library_dir)),
                    source_name=source_name,
                    source_is_dir=self.torrent_source_is_dir(torrent),
                )
        except Exception as error:
            self.show_error(f"Move suggestion failed: {error}", True)
            return
        self.pending_move_torrent = dict(torrent)
        self.push_screen(
            MoveScreen(
                current_dir=source_dir,
                current_name=source_name,
                suggestion=suggestion,
            ),
            self.handle_move_request,
        )

    def handle_move_request(self, request: MoveRequest | None) -> None:
        if request is None:
            self.pending_move_torrent = None
            return
        torrent = self.pending_move_torrent
        if not torrent:
            return
        dest_dir = request.dest_dir.strip()
        filename = request.filename.strip()
        if not dest_dir or not filename:
            self.show_error("Move failed: destination directory and filename are required.", True)
            self.pending_move_torrent = None
            return

        try:
            dest_exists = self.remote.path_exists(dest_dir) if self.remote else self.local_dest_path(dest_dir).exists()
        except Exception as error:
            self.show_error(f"Move failed: could not check destination directory: {error}", True)
            self.pending_move_torrent = None
            return

        if not dest_exists:
            self.pending_move_request = request
            self.push_screen(
                ConfirmScreen(f"Create destination directory {dest_dir}?"),
                self.handle_create_dir_confirmed,
            )
            return

        self.pending_move_request = None
        self.move_completed_torrent(torrent, request, create_dir=False)

    def handle_create_dir_confirmed(self, confirmed) -> None:
        request = self.pending_move_request
        self.pending_move_request = None
        torrent = self.pending_move_torrent
        if not confirmed or request is None or torrent is None:
            self.pending_move_torrent = None
            return
        self.move_completed_torrent(torrent, request, create_dir=True)

    def action_cycle_sort(self) -> None:
        index = SORT_MODES.index(self.sort_mode)
        self.sort_mode = SORT_MODES[(index + 1) % len(SORT_MODES)]
        self.render_results()

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_show_error_details(self) -> None:
        details = self.error_details_text()
        if details:
            self.push_screen(NoticeScreen(details, copyable=True))

    def dispatch_search(self, query: str) -> None:
        self.search_request_id += 1
        search_id = self.search_request_id
        media_key = self.current_media_key
        self.start_search(query, media_key)
        self.run_search(query, media_key, search_id)

    @work(thread=True)
    def run_search(self, query: str, media_key: str, search_id: int) -> None:
        if self.remote:
            try:
                results, errors = self.remote.search(self.config.media_types[media_key], query)
            except Exception as error:
                self.call_from_thread(
                    self.finish_search_failure,
                    f"Search failed: {error}",
                    media_key,
                    search_id,
                )
                return
            self.call_from_thread(self.set_results, results, errors, media_key, search_id)
            return

        media = self.config.media_types[media_key]
        catalog = self.jackett_indexers
        if not catalog:
            try:
                catalog = fetch_indexers(self.api_key, self.config.jackett_base_url, self.config.timeout)
            except Exception as error:
                self.call_from_thread(
                    self.finish_search_failure,
                    f"Jackett indexer check failed: {error}",
                    media_key,
                    search_id,
                )
                return
            self.call_from_thread(self.set_indexer_catalog, catalog)

        active_indexers, config_errors = usable_indexers(media.indexers, media.categories, catalog)
        if not active_indexers:
            self.call_from_thread(self.set_results, [], config_errors, media_key, search_id)
            return

        self.call_from_thread(
            self.update_status,
            f"Searching {media.label}: {query!r}...",
        )
        results, errors = search_indexers(
            query,
            active_indexers,
            media.categories,
            self.api_key,
            self.config.jackett_base_url,
            self.config.timeout,
        )
        self.call_from_thread(self.set_results, results, config_errors | errors, media_key, search_id)

    def finish_search_failure(self, message: str, media_key: str, search_id: int) -> None:
        if search_id != self.search_request_id or media_key != self.current_media_key:
            return
        self.show_error(message, True)
        self.set_results([], {}, media_key, search_id)

    @work(thread=True)
    def reconcile_indexers(self) -> None:
        self.call_from_thread(self.update_status, "Checking Jackett indexers...")
        if self.remote:
            try:
                data = self.remote.reconcile_indexers(self.config.media_types)
            except Exception as error:
                self.call_from_thread(self.show_error, f"Jackett indexer check failed: {error}", False)
                return
            catalog = jackett_catalog_from_remote(data.get("catalog", {}))
            self.call_from_thread(
                self.set_indexer_reconciliation,
                catalog,
                list(data.get("report_lines", [])),
                int(data.get("enabled_count") or 0),
                int(data.get("failure_count") or 0),
            )
            return

        desired = desired_indexers_by_media(self.config.media_types)
        try:
            catalog = fetch_indexers(self.api_key, self.config.jackett_base_url, self.config.timeout)
        except Exception as error:
            self.call_from_thread(self.show_error, f"Jackett indexer check failed: {error}", False)
            return

        catalog, enabled, failed = self.enable_missing_indexers(tuple(desired), catalog)
        report_lines = indexer_reconciliation_report(desired, catalog, enabled, failed)
        failure_count = indexer_reconciliation_failure_count(desired, catalog, failed)
        self.call_from_thread(
            self.set_indexer_reconciliation,
            catalog,
            report_lines,
            len(enabled),
            failure_count,
        )

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

        self.call_from_thread(
            self.update_status,
            f"Enabling Jackett indexer(s): {', '.join(missing)}...",
        )
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
            catalog = fetch_indexers(self.api_key, self.config.jackett_base_url, self.config.timeout)
        except Exception as error:
            errors["jackett"] = f"could not refresh indexer list: {error}"
            return catalog, enabled, errors

        return catalog, enabled, errors

    @work(thread=True)
    def refresh_indexer_catalog(self) -> None:
        if self.remote:
            self.reconcile_indexers()
            return
        try:
            catalog = fetch_indexers(self.api_key, self.config.jackett_base_url, self.config.timeout)
        except Exception as error:
            self.call_from_thread(self.show_error, f"Jackett indexer check failed: {error}", False)
            return
        self.call_from_thread(self.set_indexer_catalog, catalog)

    @work(thread=True)
    def add_download(self, result: SearchResult) -> None:
        self.call_from_thread(self.update_status, f"Adding {result.title!r} to Transmission...")
        try:
            if self.remote:
                torrent = self.remote.add_download(result)
            else:
                arguments = build_torrent_add_arguments(
                    result,
                    self.config.transmission_jackett_base_url,
                    self.config.jackett_base_url,
                    self.config.timeout,
                )
                torrent = TransmissionClient(self.config.transmission_rpc_url).add_torrent(arguments)
        except Exception as error:
            self.call_from_thread(self.show_error, f"Transmission add failed: {error}", True)
            return
        self.call_from_thread(self.set_download_added, result, torrent)

    @work(thread=True)
    def refresh_progress(self) -> None:
        try:
            torrents = self.remote.torrents() if self.remote else TransmissionClient(self.config.transmission_rpc_url).get_torrents()
        except Exception as error:
            self.call_from_thread(self.show_error, f"Transmission progress failed: {error}", False)
            return
        self.call_from_thread(self.set_progress, torrents)

    @work(thread=True)
    def control_torrent(self, action: str, torrent_id: int | None, torrent_name: str) -> None:
        if torrent_id is None:
            return
        try:
            if self.remote:
                torrents = self.remote.control_torrent(action, torrent_id)
            else:
                client = TransmissionClient(self.config.transmission_rpc_url)
                if action == "start":
                    client.start_torrents([torrent_id])
                elif action == "stop":
                    client.stop_torrents([torrent_id])
                elif action == "remove_destroy":
                    client.remove_torrents([torrent_id], delete_local_data=True)
                torrents = client.get_torrents()
        except Exception as error:
            self.call_from_thread(self.show_error, f"Transmission control failed: {error}", True)
            return
        self.call_from_thread(self.set_torrent_action_result, action, torrent_id, torrent_name, torrents)

    @work(thread=True)
    def move_completed_torrent(self, torrent: dict, request: MoveRequest, create_dir: bool) -> None:
        try:
            if self.remote:
                torrents, dest_dir, filename = self.remote_move_completed_torrent(torrent, request, create_dir)
            else:
                source_dir = torrent.get("downloadDir") or ""
                source_name = torrent.get("name") or ""
                if not source_dir or not source_name:
                    raise RuntimeError("missing torrent source path")

                source_local = self.transmission_path_to_local(source_dir) / source_name
                dest_dir_local = self.local_dest_path(request.dest_dir)
                dest_local = dest_dir_local / request.filename

                if create_dir:
                    dest_dir_local.mkdir(parents=True, exist_ok=True)
                elif not dest_dir_local.exists():
                    raise FileNotFoundError(f"destination directory does not exist: {request.dest_dir}")

                if not source_local.exists():
                    raise FileNotFoundError(f"source path does not exist: {source_local}")
                if source_local == dest_local:
                    raise RuntimeError("source and destination are the same")
                if dest_local.exists():
                    raise FileExistsError(f"destination already exists: {dest_local}")

                shutil.move(str(source_local), str(dest_local))

                client = TransmissionClient(self.config.transmission_rpc_url)
                client.remove_torrents([torrent.get("id")], delete_local_data=False)
                torrents = client.get_torrents()
                dest_dir = request.dest_dir
                filename = request.filename
        except Exception as error:
            self.call_from_thread(self.finish_move_error, error)
            return

        self.call_from_thread(
            self.finish_move_success,
            torrents,
            dest_dir,
            filename,
        )

    def remote_move_completed_torrent(
        self,
        torrent: dict,
        request: MoveRequest,
        create_dir: bool,
    ) -> tuple[list[dict], str, str]:
        assert self.remote is not None
        try:
            data = self.remote.move_completed_torrent(torrent, request.dest_dir, request.filename, create_dir)
            return (
                list(data.get("torrents", [])),
                str(data.get("dest_dir") or request.dest_dir),
                str(data.get("filename") or request.filename),
            )
        except Exception as error:
            recovered = self.recover_timed_out_move(torrent, request, error)
            if recovered is not None:
                return recovered
            raise

    def recover_timed_out_move(
        self,
        torrent: dict,
        request: MoveRequest,
        error: Exception,
    ) -> tuple[list[dict], str, str] | None:
        if not self.is_timeout_error(error):
            return None

        torrent_id = torrent.get("id")
        dest_local = self.local_dest_path(request.dest_dir) / request.filename
        for _ in range(15):
            if not dest_local.exists():
                time.sleep(1)
                continue
            try:
                torrents = self.remote.torrents() if self.remote else []
            except Exception:
                time.sleep(1)
                continue
            if any(item.get("id") == torrent_id for item in torrents):
                time.sleep(1)
                continue
            return torrents, request.dest_dir, request.filename
        return None

    def is_timeout_error(self, error: Exception) -> bool:
        if isinstance(error, (TimeoutError, socket.timeout)):
            return True
        return "timed out" in str(error).lower()

    def set_results(
        self,
        results: list[SearchResult],
        errors: dict[str, str],
        media_key: str,
        search_id: int,
    ) -> None:
        if search_id != self.search_request_id or media_key != self.current_media_key:
            return
        self.search_in_flight = False
        self.results = results
        self.errors = errors
        if errors:
            self.set_notice(f"Search errors on {len(errors)} indexer(s). Press e for details.", "search")
            self.message = f"Search complete: {len(results)} result(s), {len(errors)} indexer error(s)."
        else:
            self.clear_notice("search")
            self.message = f"Search complete: {len(results)} result(s)."
        self.render_results()

    def set_indexer_catalog(self, catalog: dict[str, JackettIndexer]) -> None:
        self.jackett_indexers = catalog
        self.update_transmission_title()

    def set_indexer_reconciliation(
        self,
        catalog: dict[str, JackettIndexer],
        report_lines: list[str],
        enabled_count: int,
        failure_count: int,
    ) -> None:
        self.jackett_indexers = catalog
        self.indexer_report_lines = report_lines
        if failure_count:
            self.set_notice(
                f"Jackett indexers: enabled {enabled_count}, failed {failure_count}. Press e for details.",
                "jackett",
            )
            self.message = "Jackett indexer check completed with errors."
        elif enabled_count:
            self.clear_notice("jackett")
            self.message = f"Jackett indexers: enabled {enabled_count}. Press e for details."
        else:
            self.clear_notice("jackett")
            self.message = "Jackett indexers ready. Press e for details."
        self.update_notice()
        self.update_title()
        self.update_transmission_title()

    def set_download_added(self, result: SearchResult, torrent: dict) -> None:
        self.render_results()
        name = torrent.get("name") or result.title
        self.update_status(f"Added to Transmission: {name}")

    def set_progress(self, torrents: list[dict]) -> None:
        self.torrents = torrents
        self.clear_notice("transmission_progress")
        self.render_results()
        self.render_torrents()
        self.update_notice()

    def set_torrent_action_result(
        self,
        action: str,
        torrent_id: int,
        torrent_name: str,
        torrents: list[dict],
    ) -> None:
        self.torrents = torrents
        self.render_results()
        self.render_torrents()
        self.update_notice()
        torrent = next((item for item in torrents if item.get("id") == torrent_id), None)
        if action == "start":
            if torrent and torrent.get("status") != 0:
                self.update_status(f"Resumed: {torrent_name or torrent_id}")
            else:
                self.show_error(self.resume_failure_message(torrent_name or str(torrent_id), torrent), True)
        elif action == "stop":
            if torrent and torrent.get("status") == 0:
                self.update_status(f"Paused: {torrent_name or torrent_id}")
            else:
                self.show_error(f"Transmission pause did not change state for {torrent_name or torrent_id}.", True)

    def finish_move_success(self, torrents: list[dict], dest_dir: str, filename: str) -> None:
        self.pending_move_torrent = None
        self.torrents = torrents
        self.render_results()
        self.render_torrents()
        self.update_status(
            f"Archived to {dest_dir.rstrip('/')}/{filename} and no longer tracked by Transmission."
        )

    def finish_move_error(self, error: Exception) -> None:
        self.pending_move_torrent = None
        self.show_error(f"Move failed: {error}", True)

    def render_results(self) -> None:
        previous = self.current_result()
        table = self.query_one("#results", DataTable)
        table.clear(columns=True)
        self.setup_results_table()

        self.visible_results = self.sorted_results()
        for result in self.visible_results:
            table.add_row(
                str(result.seeders),
                str(result.leechers),
                result.size,
                display_name(result.indexer),
                result.title,
                key=result.identity,
            )

        if previous:
            self.restore_cursor(previous.identity)

        self.update_notice()
        self.update_title()
        self.render_torrents()

    def render_torrents(self) -> None:
        table = self.query_one("#transmission", DataTable)
        previous = self.current_torrent_id()
        table.clear()

        for torrent in self.sorted_torrents():
            torrent_id = torrent.get("id")
            table.add_row(
                TORRENT_STATUS.get(torrent.get("status"), str(torrent.get("status"))),
                format_size(torrent.get("totalSize")),
                format_percent(torrent.get("percentDone")),
                format_eta(torrent.get("eta")),
                format_rate(torrent.get("rateDownload")),
                format_peers(torrent),
                torrent.get("name") or "",
                key=str(torrent_id),
            )

        if previous is not None:
            for index, torrent in enumerate(self.sorted_torrents()):
                if torrent.get("id") == previous:
                    table.move_cursor(row=index, animate=False)
                    break

    def focused_table(self) -> DataTable:
        if isinstance(self.focused, DataTable) and self.focused.id == "transmission":
            return self.query_one("#transmission", DataTable)
        return self.query_one("#results", DataTable)

    def sorted_torrents(self) -> list[dict]:
        return sorted(self.torrents, key=lambda item: item.get("id") or 0)

    def sorted_results(self) -> list[SearchResult]:
        if self.sort_mode == "seeders":
            results = sorted(self.results, key=lambda result: result.seeders_value, reverse=True)
        elif self.sort_mode == "leechers":
            results = sorted(self.results, key=lambda result: result.leechers_value, reverse=True)
        elif self.sort_mode == "size":
            results = sorted(self.results, key=lambda result: result.size_bytes, reverse=True)
        elif self.sort_mode == "indexer":
            results = sorted(self.results, key=lambda result: (result.indexer, result.title.lower()))
        elif self.sort_mode == "title":
            results = sorted(self.results, key=lambda result: result.title.lower())
        else:
            results = sorted(self.results, key=lambda result: result.seeders_value, reverse=True)
        return results

    def current_result(self) -> SearchResult | None:
        if not self.visible_results:
            return None
        table = self.query_one("#results", DataTable)
        row = max(0, min(table.cursor_coordinate.row, len(self.visible_results) - 1))
        return self.visible_results[row]

    def current_torrent(self) -> dict | None:
        if not self.torrents:
            return None
        table = self.query_one("#transmission", DataTable)
        row = max(0, min(table.cursor_coordinate.row, len(self.torrents) - 1))
        return self.sorted_torrents()[row]

    def current_torrent_id(self) -> int | None:
        torrent = self.current_torrent()
        if torrent:
            return torrent.get("id")
        return None

    def current_media(self):
        return self.config.media_types[self.current_media_key]

    def switch_media_type(self, offset: int) -> None:
        if not self.media_type_keys:
            return
        current_index = self.media_type_keys.index(self.current_media_key)
        self.switch_media_type_to(self.media_type_keys[(current_index + offset) % len(self.media_type_keys)])

    def switch_media_type_to(self, media_key: str) -> None:
        if media_key not in self.config.media_types:
            return
        self.current_media_key = media_key
        self.results = []
        self.visible_results = []
        self.errors = {}
        self.notice_message = None
        self.notice_kind = None
        self.search_in_flight = False
        self.message = f"Switched to {self.current_media().label}."
        self.query_one("#media-type", Select).value = self.current_media_key
        self.query_one("#search", Input).placeholder = (
            f"Search {self.current_media().label.lower()} across configured Jackett indexers"
        )
        self.render_results()
        self.update_notice()
        self.update_title()
        self.update_transmission_title()
        if self.query:
            self.dispatch_search(self.query)

    def restore_cursor(self, identity: str) -> None:
        for index, result in enumerate(self.visible_results):
            if result.identity == identity:
                self.query_one("#results", DataTable).move_cursor(row=index, animate=False)
                return

    def update_status(self, message: str | None = None) -> None:
        if message:
            self.message = message
        self.update_title()

    def copy_text_to_clipboard(self, text: str) -> None:
        try:
            copy_to_clipboard(text)
        except Exception as error:
            self.show_error(f"Clipboard copy failed: {error}", False)
            return
        self.message = "Copied modal text to clipboard."
        self.update_title()

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

    def show_error(self, message: str, modal: bool) -> None:
        self.set_notice(message, notice_kind_for_message(message))
        self.update_notice()
        self.update_title()
        self.update_transmission_title()
        if modal:
            self.push_screen(NoticeScreen(message, copyable=True))

    def set_notice(self, message: str, kind: str) -> None:
        self.notice_message = message
        self.notice_kind = kind

    def clear_notice(self, kind: str | None = None) -> None:
        if kind is not None and self.notice_kind != kind:
            return
        self.notice_message = None
        self.notice_kind = None
        self.update_notice()

    def update_notice(self) -> None:
        notice = self.query_one("#notice", Static)
        notice.update(self.notice_message or "")

    def error_details_text(self) -> str:
        if self.errors:
            return "\n".join(
                f"{display_name(indexer)}: {message}"
                for indexer, message in sorted(self.errors.items())
            )
        if self.indexer_report_lines:
            return "\n".join(self.indexer_report_lines)
        if self.notice_message:
            return self.notice_message
        return ""

    def update_title(self) -> None:
        title = self.query_one("#results-title", Static)
        summary = ""
        if self.message:
            summary = self.message
        elif isinstance(self.focused, Input):
            summary = "Enter search term and press Enter."
        elif isinstance(self.focused, Select):
            summary = "Choose media type. Enter opens the list. / focuses search."
        elif isinstance(self.focused, DataTable) and self.focused.id == "transmission":
            summary = "Keys: j/k move, Space pause, m move, x remove, r refresh, e errors"
        else:
            summary = "Keys: j/k move, a add, o sort, t transmission, e errors"
        title.update(summary)

    def update_transmission_title(self) -> None:
        title = self.query_one("#transmission-title", Static)
        if self.remote:
            summary = f"Transmission via Curator: {self.remote.base_url}"
        else:
            summary = f"Transmission: {transmission_title_url(self.config.transmission_rpc_url)}"
        if isinstance(self.focused, DataTable) and self.focused.id == "transmission":
            summary = f"{summary} | Keys: Space pause, m move, x remove, r refresh"
        title.update(summary)

    def setup_results_table(self) -> None:
        table = self.query_one("#results", DataTable)
        table.add_columns(*self.result_column_labels())
        table.set_class(self.search_in_flight, "-stale")

    def result_column_labels(self) -> list[str]:
        labels: list[str] = []
        for label, key in RESULT_COLUMNS:
            if key == self.sort_mode:
                labels.append(f"[{label}]")
            else:
                labels.append(label)
        return labels

    def start_search(self, query: str, media_key: str) -> None:
        self.query = query
        self.search_in_flight = True
        media = self.config.media_types[media_key]
        self.message = f"Searching {media.label}: {query!r}..."
        self.update_title()
        self.setup_results_table()

    def resume_failure_message(self, torrent_name: str, torrent: dict | None) -> str:
        if torrent is None:
            return f"Transmission resume failed for {torrent_name}: torrent was not returned after refresh."
        state = TORRENT_STATUS.get(torrent.get("status"), str(torrent.get("status")))
        error_text = (torrent.get("errorString") or "").strip()
        if error_text:
            return f"Transmission resume failed for {torrent_name}: still {state}. {error_text}"
        return (
            f"Transmission resume failed for {torrent_name}: still {state} after refresh. "
            "Check Transmission for queue limits or tracker errors."
        )


def jackett_catalog_from_remote(data: dict) -> dict[str, JackettIndexer]:
    return {
        str(key): JackettIndexer(
            id=str(value["id"]),
            title=str(value["title"]),
            configured=bool(value.get("configured")),
            categories=tuple(str(item) for item in value.get("categories", ())),
            search_types=tuple(str(item) for item in value.get("search_types", ())),
        )
        for key, value in data.items()
    }


def format_percent(value) -> str:
    if isinstance(value, (int, float)):
        if value >= 1:
            return "100%"
        if value > 0.99:
            return "99%"
        return f"{int(value * 100 + 0.5)}%"
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


def notice_kind_for_message(message: str) -> str:
    if message.startswith("Transmission progress failed:"):
        return "transmission_progress"
    if message.startswith("Transmission"):
        return "transmission"
    if message.startswith("Jackett"):
        return "jackett"
    if message.startswith("Search"):
        return "search"
    if message.startswith("Move"):
        return "move"
    return "general"


def transmission_title_url(rpc_url: str) -> str:
    parsed = urlparse(rpc_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/rpc"):
        path = path[:-4]
    return parsed._replace(path=path).geturl()


def copy_to_clipboard(text: str) -> None:
    for command in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(command[0]):
            subprocess.run(command, input=text, text=True, check=True)
            return
    raise RuntimeError("install wl-copy, xclip, or xsel")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Curator torrent picker.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("curator/client.toml"),
        help="Client TOML config path (default: curator/client.toml)",
    )
    parser.add_argument("--server-url", help="Curator server URL, for example http://127.0.0.1:8787")
    parser.add_argument("--token", help="Bearer token for Curator server")
    parser.add_argument("--remote-timeout", type=float, help="Remote server request timeout")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    server_url = args.server_url or config.server_url
    if server_url:
        token = args.token if args.token is not None else config.server_token
        request_timeout = (
            args.remote_timeout
            if args.remote_timeout is not None
            else config.server_request_timeout
        )
        remote = CuratorRemoteClient(server_url, token, request_timeout, config.timeout)
        CuratorApp(config, remote).run()
    else:
        CuratorApp(config).run()
