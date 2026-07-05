from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import PurePosixPath
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlparse

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static

from .client import (
    build_torrent_add_arguments,
    SearchResult,
    TransmissionClient,
    display_name,
    format_size,
    load_api_key,
    load_state,
    mark_download,
    save_state,
    search_indexers,
    update_download_progress,
)
from .config import AppConfig, DEFAULT_SORT, load_config


SORT_MODES = ("seeders", "indexer", "title")
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
j/k      move down/up
g/G      first/last row
r        refresh focused pane
a        add selected search result
Space    pause/resume selected torrent
x        remove selected torrent and local data, with confirmation
m        move completed torrent to library
o        cycle result sort
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

    def __init__(self, current_dir: str, current_name: str, dest_dir: str, filename: str):
        super().__init__()
        self.current_dir = current_dir
        self.current_name = current_name
        self.dest_dir = dest_dir
        self.filename = filename

    def compose(self) -> ComposeResult:
        with Vertical(id="move-dialog"):
            yield Static("Archive completed torrent to a new location and stop tracking it in Transmission.")
            yield Static(f"Current dir: {self.current_dir}")
            yield Static(f"Current name: {self.current_name}")
            yield Static("Destination dir", classes="move-label")
            yield Input(self.dest_dir, id="move-dest-dir")
            yield Static("Final name", classes="move-label")
            yield Input(self.filename, id="move-filename")
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

    #search {
        dock: top;
        margin: 0 1;
    }

    #alert {
        height: auto;
        padding: 0 1;
        color: $text;
        background: $error-darken-2;
    }

    #results-title {
        height: 1;
        padding: 0 1;
    }

    #results {
        height: 2fr;
    }

    #transmission-pane {
        height: 1fr;
        border-top: solid $primary;
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
        Binding("question_mark", "show_help", "Help"),
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.api_key = load_api_key(config.jackett_config_dir)
        self.state = load_state(config.state_file)
        self.results: list[SearchResult] = []
        self.visible_results: list[SearchResult] = []
        self.torrents: list[dict] = []
        self.query = ""
        self.sort_mode = config.default_sort if config.default_sort in SORT_MODES else DEFAULT_SORT
        self.errors: dict[str, str] = {}
        self.message: str | None = None
        self.alert_message: str | None = None
        self.pending_remove_torrent_id: int | None = None
        self.pending_move_torrent: dict | None = None
        self.pending_move_request: MoveRequest | None = None

    def compose(self) -> ComposeResult:
        placeholder = f"Search {self.config.media.label.lower()} across configured Jackett indexers"
        yield Header()
        yield Input(placeholder=placeholder, id="search")
        yield Static("", id="alert")
        yield Static("", id="results-title")
        yield DataTable(id="results")
        with Vertical(id="transmission-pane"):
            yield Static(
                (
                    f"Transmission: {transmission_title_url(self.config.transmission_rpc_url)} | "
                    f"Media: {self.config.media.label} | "
                    f"Library: {self.config.media.library_dir}"
                ),
                id="transmission-title",
            )
            yield DataTable(id="transmission")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Seed", "Leech", "Size", "Indexer", "Title")
        torrents = self.query_one("#transmission", DataTable)
        torrents.cursor_type = "row"
        torrents.zebra_stripes = True
        torrents.add_columns("ID", "State", "Size", "Progress", "ETA", "DL", "Peers", "Name")
        self.query_one("#search", Input).focus()
        self.update_alert()
        self.update_title()
        self.update_transmission_title()
        self.set_interval(5, self.refresh_progress)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            self.query = query
            self.run_search(query)

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
            self.run_search(self.query)

    def action_add_download(self) -> None:
        result = self.current_result()
        if result:
            self.add_download(result)

    def action_toggle_torrent_pause(self) -> None:
        torrent = self.current_torrent()
        if not torrent:
            return
        if torrent.get("status") == 0:
            self.control_torrent("start", torrent.get("id"))
        else:
            self.control_torrent("stop", torrent.get("id"))

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
            self.control_torrent("remove_destroy", torrent_id)

    def action_move_completed(self) -> None:
        torrent = self.current_torrent()
        if not torrent:
            return
        if torrent.get("leftUntilDone") != 0 and torrent.get("percentDone") != 1:
            self.push_screen(NoticeScreen("Move skipped: selected torrent is not complete."))
            return
        self.pending_move_torrent = dict(torrent)
        self.push_screen(
            MoveScreen(
                current_dir=torrent.get("downloadDir") or "",
                current_name=torrent.get("name") or "",
                dest_dir=str(self.config.media.library_dir),
                filename=torrent.get("name") or "",
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

        dest_dir_local = self.local_dest_path(dest_dir)
        if not dest_dir_local.exists():
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

    @work(thread=True)
    def run_search(self, query: str) -> None:
        self.call_from_thread(
            self.update_status,
            f"Searching {', '.join(self.config.media.indexers)} for {query!r}...",
        )
        results, errors = search_indexers(
            query,
            self.config.media.indexers,
            self.api_key,
            self.config.jackett_base_url,
            self.config.timeout,
        )
        self.call_from_thread(self.set_results, results, errors)

    @work(thread=True)
    def add_download(self, result: SearchResult) -> None:
        self.call_from_thread(self.update_status, f"Adding {result.title!r} to Transmission...")
        try:
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
            torrents = TransmissionClient(self.config.transmission_rpc_url).get_torrents()
        except Exception as error:
            self.call_from_thread(self.show_error, f"Transmission progress failed: {error}", False)
            return
        self.call_from_thread(self.set_progress, torrents)

    @work(thread=True)
    def control_torrent(self, action: str, torrent_id: int | None) -> None:
        if torrent_id is None:
            return
        client = TransmissionClient(self.config.transmission_rpc_url)
        try:
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
        self.call_from_thread(self.set_progress, torrents)

    @work(thread=True)
    def move_completed_torrent(self, torrent: dict, request: MoveRequest, create_dir: bool) -> None:
        try:
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
        except Exception as error:
            self.call_from_thread(self.finish_move_error, error)
            return

        self.call_from_thread(
            self.finish_move_success,
            torrents,
            request.dest_dir,
            request.filename,
        )

    def set_results(self, results: list[SearchResult], errors: dict[str, str]) -> None:
        self.results = results
        self.errors = errors
        self.message = None
        if errors:
            details = ", ".join(f"{display_name(key)}={value}" for key, value in errors.items())
            self.alert_message = f"Indexer errors: {details}"
        else:
            self.alert_message = None
        self.render_results()

    def set_download_added(self, result: SearchResult, torrent: dict) -> None:
        mark_download(self.state, result, torrent)
        save_state(self.state, self.config.state_file)
        self.alert_message = None
        self.render_results()
        name = torrent.get("name") or result.title
        self.update_status(f"Added to Transmission: {name}")

    def set_progress(self, torrents: list[dict]) -> None:
        self.torrents = torrents
        update_download_progress(self.state, torrents)
        save_state(self.state, self.config.state_file)
        self.alert_message = None
        self.render_results()
        self.render_torrents()
        self.update_alert()

    def finish_move_success(self, torrents: list[dict], dest_dir: str, filename: str) -> None:
        self.pending_move_torrent = None
        self.alert_message = None
        self.torrents = torrents
        update_download_progress(self.state, torrents)
        save_state(self.state, self.config.state_file)
        self.render_results()
        self.render_torrents()
        self.update_alert()
        self.update_status(
            f"Archived to {dest_dir.rstrip('/')}/{filename} and no longer tracked by Transmission."
        )

    def finish_move_error(self, error: Exception) -> None:
        self.pending_move_torrent = None
        self.show_error(f"Move failed: {error}", True)

    def render_results(self) -> None:
        previous = self.current_result()
        table = self.query_one("#results", DataTable)
        table.clear()

        self.visible_results = self.sorted_results()[:self.config.max_results]
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

        self.update_alert()
        self.update_title()
        self.render_torrents()

    def render_torrents(self) -> None:
        table = self.query_one("#transmission", DataTable)
        previous = self.current_torrent_id()
        table.clear()

        for torrent in self.sorted_torrents():
            torrent_id = torrent.get("id")
            table.add_row(
                str(torrent_id or ""),
                TORRENT_STATUS.get(torrent.get("status"), str(torrent.get("status"))),
                format_size(torrent.get("totalSize")),
                format_percent(torrent.get("percentDone")),
                format_eta(torrent.get("eta")),
                format_rate(torrent.get("rateDownload")),
                str(torrent.get("peersConnected") or 0),
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
            return sorted(self.results, key=lambda result: result.seeders_value, reverse=True)
        if self.sort_mode == "indexer":
            return sorted(self.results, key=lambda result: (result.indexer, result.title.lower()))
        if self.sort_mode == "title":
            return sorted(self.results, key=lambda result: result.title.lower())
        return sorted(self.results, key=lambda result: result.seeders_value, reverse=True)

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

    def show_error(self, message: str, modal: bool) -> None:
        self.message = message
        self.alert_message = message
        self.update_alert()
        self.update_title()
        self.update_transmission_title()
        if modal:
            self.push_screen(NoticeScreen(message, copyable=True))

    def clear_alert(self) -> None:
        self.alert_message = None
        self.update_alert()

    def update_alert(self) -> None:
        alert = self.query_one("#alert", Static)
        alert.update(self.alert_message or "")

    def update_title(self) -> None:
        title = self.query_one("#results-title", Static)
        query = self.query or "none"
        summary = (
            f"Media: {self.config.media.label} | Search: {query!r} | "
            f"Sort: {self.sort_mode} | Showing {len(self.visible_results)}/{len(self.results)}"
        )
        if self.message:
            summary = f"{summary} | {self.message}"
        elif isinstance(self.focused, Input):
            summary = f"{summary} | Enter search term and press Enter."
        elif isinstance(self.focused, DataTable) and self.focused.id == "transmission":
            summary = f"{summary} | Keys: j/k move, Space pause, m move, x remove, r refresh"
        else:
            summary = f"{summary} | Keys: j/k move, a add, o sort, t transmission, / search"
        title.update(summary)

    def update_transmission_title(self) -> None:
        title = self.query_one("#transmission-title", Static)
        summary = (
            f"Transmission: {transmission_title_url(self.config.transmission_rpc_url)} | "
            f"Media: {self.config.media.label} | "
            f"Library: {self.config.media.library_dir}"
        )
        if isinstance(self.focused, DataTable) and self.focused.id == "transmission":
            summary = f"{summary} | Keys: Space pause, m move, x remove, r refresh"
        title.update(summary)


def format_percent(value) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.0%}"
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
        default=Path("curator/config.toml"),
        help="TOML config path (default: curator/config.toml)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    CuratorApp(config).run()
