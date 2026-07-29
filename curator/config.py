from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib


DEFAULT_BASE_URL = "http://127.0.0.1:9117"
DEFAULT_TRANSMISSION_JACKETT_BASE_URL = "http://jackett:9117"
DEFAULT_TRANSMISSION_RPC_URL = "http://127.0.0.1:9091/transmission/rpc"
DEFAULT_TRANSMISSION_WEB_URL = "http://localhost:9091"
DEFAULT_JACKETT_CONFIG_DIR = Path("../jackett-config/Jackett")
DEFAULT_GLUETUN_STATE_PATH = Path("../gluetun/vpngate-state.json")
DEFAULT_SORT = "seeders"
DEFAULT_MEDIA_TYPE = "movies"
DEFAULT_TIMEOUT = 70.0


@dataclass(frozen=True)
class MediaTypeConfig:
    key: str
    label: str
    indexers: tuple[str, ...]
    categories: tuple[str, ...]
    library_dir: Path


@dataclass(frozen=True)
class CuratorConfig:
    config_path: Path
    downloads_root: Path
    library_root: Path
    jackett_base_url: str
    transmission_jackett_base_url: str
    jackett_admin_password: str
    transmission_rpc_url: str
    transmission_web_url: str
    jackett_config_dir: Path
    gluetun_state_path: Path
    timeout: float
    default_sort: str
    default_media_type: str
    media_types: dict[str, MediaTypeConfig]


def load_config(config_path: Path) -> CuratorConfig:
    data = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    base_dir = config_path.parent

    jackett = data.get("jackett", {})
    network = data.get("network", {})
    paths = data.get("paths", {})
    transmission = data.get("transmission", {})
    ui = data.get("ui", {})
    media_types = data.get("media_types", {})
    parsed_media_types = parse_media_types(media_types, config_path)
    default_media_type = str(ui.get("default_media_type", DEFAULT_MEDIA_TYPE))
    if default_media_type not in parsed_media_types and parsed_media_types:
        default_media_type = next(iter(parsed_media_types))

    return CuratorConfig(
        config_path=config_path,
        downloads_root=_resolve_path(
            base_dir,
            os.environ.get("CURATOR_DOWNLOADS_ROOT", str(paths.get("downloads_root", "../downloads"))),
        ),
        library_root=_resolve_path(
            base_dir,
            os.environ.get("CURATOR_LIBRARY_ROOT", str(paths.get("library_root", "../library"))),
        ),
        jackett_base_url=os.environ.get(
            "CURATOR_JACKETT_BASE_URL",
            str(jackett.get("base_url", DEFAULT_BASE_URL)),
        ),
        transmission_jackett_base_url=os.environ.get(
            "CURATOR_TRANSMISSION_JACKETT_BASE_URL",
            str(jackett.get("transmission_base_url", DEFAULT_TRANSMISSION_JACKETT_BASE_URL)),
        ),
        jackett_admin_password=os.environ.get(
            "CURATOR_JACKETT_ADMIN_PASSWORD",
            str(jackett.get("admin_password", "")),
        ),
        transmission_rpc_url=os.environ.get(
            "CURATOR_TRANSMISSION_RPC_URL",
            str(transmission.get("rpc_url", DEFAULT_TRANSMISSION_RPC_URL)),
        ),
        transmission_web_url=os.environ.get(
            "CURATOR_TRANSMISSION_WEB_URL",
            str(transmission.get("web_url", DEFAULT_TRANSMISSION_WEB_URL)),
        ),
        jackett_config_dir=_resolve_path(
            base_dir,
            os.environ.get(
                "CURATOR_JACKETT_CONFIG_DIR",
                str(jackett.get("config_dir", str(DEFAULT_JACKETT_CONFIG_DIR))),
            ),
        ),
        gluetun_state_path=_resolve_path(
            base_dir,
            os.environ.get(
                "CURATOR_GLUETUN_STATE_PATH",
                str(paths.get("gluetun_state_path", str(DEFAULT_GLUETUN_STATE_PATH))),
            ),
        ),
        timeout=float(os.environ.get("CURATOR_NETWORK_TIMEOUT", network.get("timeout", DEFAULT_TIMEOUT))),
        default_sort=str(ui.get("default_sort", DEFAULT_SORT)),
        default_media_type=default_media_type,
        media_types=parsed_media_types,
    )


def parse_media_types(raw_media_types, config_path: Path) -> dict[str, MediaTypeConfig]:
    if not raw_media_types:
        raise ValueError(f"No media types defined in {config_path}")
    return {
        key: MediaTypeConfig(
            key=key,
            label=str(value.get("label", key.replace("_", " ").title())),
            indexers=tuple(_parse_indexers(value.get("indexers", ()))),
            categories=tuple(_parse_categories(value.get("categories", ()))),
            library_dir=Path(str(value["library_dir"])),
        )
        for key, value in raw_media_types.items()
    }


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


def _parse_categories(raw_categories) -> list[str]:
    if isinstance(raw_categories, str):
        categories = [item.strip() for item in raw_categories.split(",") if item.strip()]
    else:
        categories = [str(item).strip() for item in raw_categories if str(item).strip()]
    return categories
