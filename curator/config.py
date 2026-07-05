from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


DEFAULT_BASE_URL = "http://127.0.0.1:9117"
DEFAULT_TRANSMISSION_JACKETT_BASE_URL = "http://jackett:9117"
DEFAULT_TRANSMISSION_RPC_URL = "http://127.0.0.1:9091/transmission/rpc"
DEFAULT_JACKETT_CONFIG_DIR = Path("../jackett-config/Jackett")
DEFAULT_STATE_FILE = Path("state.json")
DEFAULT_MAX_RESULTS = 30
DEFAULT_SORT = "seeders"
DEFAULT_MEDIA_TYPE = "movies"
DEFAULT_TIMEOUT = 20.0


@dataclass(frozen=True)
class MediaTypeConfig:
    key: str
    label: str
    indexers: tuple[str, ...]
    library_dir: Path


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    downloads_dir: Path
    jackett_base_url: str
    transmission_jackett_base_url: str
    transmission_rpc_url: str
    jackett_config_dir: Path
    state_file: Path
    timeout: float
    max_results: int
    default_sort: str
    media_type: str
    media: MediaTypeConfig


def load_config(config_path: Path) -> AppConfig:
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    base_dir = config_path.parent

    jackett = data.get("jackett", {})
    network = data.get("network", {})
    paths = data.get("paths", {})
    transmission = data.get("transmission", {})
    state = data.get("state", {})
    ui = data.get("ui", {})
    media_types = data.get("media_types", {})

    if not media_types:
        raise ValueError(f"No media types defined in {config_path}")

    selected_media_type = ui.get("default_media_type", DEFAULT_MEDIA_TYPE)
    media_data = media_types.get(selected_media_type)
    if media_data is None:
        raise KeyError(f"Media type {selected_media_type!r} not found in {config_path}")

    label = str(media_data.get("label", selected_media_type.replace("_", " ").title()))
    indexers = tuple(_parse_indexers(media_data.get("indexers", ())))
    downloads_dir = _resolve_path(base_dir, str(paths.get("downloads_dir", "../downloads")))
    library_dir = _resolve_path(base_dir, str(media_data["library_dir"]))

    return AppConfig(
        config_path=config_path,
        downloads_dir=downloads_dir,
        jackett_base_url=str(jackett.get("base_url", DEFAULT_BASE_URL)),
        transmission_jackett_base_url=str(
            jackett.get("transmission_base_url", DEFAULT_TRANSMISSION_JACKETT_BASE_URL)
        ),
        transmission_rpc_url=str(transmission.get("rpc_url", DEFAULT_TRANSMISSION_RPC_URL)),
        jackett_config_dir=_resolve_path(
            base_dir,
            str(jackett.get("config_dir", str(DEFAULT_JACKETT_CONFIG_DIR))),
        ),
        state_file=_resolve_path(base_dir, str(state.get("file", str(DEFAULT_STATE_FILE)))),
        timeout=float(network.get("timeout", DEFAULT_TIMEOUT)),
        max_results=int(ui.get("max_results", DEFAULT_MAX_RESULTS)),
        default_sort=str(ui.get("default_sort", DEFAULT_SORT)),
        media_type=selected_media_type,
        media=MediaTypeConfig(
            key=selected_media_type,
            label=label,
            indexers=indexers,
            library_dir=library_dir,
        ),
    )


def _resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return base_dir / path


def _parse_indexers(raw_indexers) -> list[str]:
    if isinstance(raw_indexers, str):
        indexers = [item.strip() for item in raw_indexers.split(",") if item.strip()]
    else:
        indexers = [str(item).strip() for item in raw_indexers if str(item).strip()]
    if not indexers:
        raise ValueError("At least one indexer is required")
    return indexers
