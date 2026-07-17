from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .client import JackettIndexer, category_matches, display_name


@dataclass(frozen=True)
class MoveSuggestion:
    dest_dir: str
    filename: str
    message: str


def usable_indexers(
    indexers: tuple[str, ...],
    categories: tuple[str, ...],
    catalog: dict[str, JackettIndexer],
) -> tuple[tuple[str, ...], dict[str, str]]:
    usable: list[str] = []
    errors: dict[str, str] = {}

    for indexer_id in indexers:
        indexer = catalog.get(indexer_id)
        if indexer is None:
            errors[indexer_id] = "not found in Jackett; check the exact Jackett definition id in curator.toml"
            continue
        if not indexer.configured:
            errors[indexer_id] = "not configured in Jackett"
            continue
        if not supports_media_categories(indexer, categories):
            errors[indexer_id] = "configured, but does not advertise matching categories"
            continue
        usable.append(indexer_id)
    return tuple(usable), errors


def desired_indexers_by_media(media_types) -> dict[str, list[str]]:
    desired: dict[str, list[str]] = {}
    for media in media_types.values():
        for indexer in media.indexers:
            desired.setdefault(indexer, []).append(media.label)
    return desired


def indexer_reconciliation_report(
    desired: dict[str, list[str]],
    catalog: dict[str, JackettIndexer],
    enabled: list[str],
    failed: dict[str, str],
) -> list[str]:
    enabled_set = set(enabled)
    failed_set = set(failed)
    missing = [indexer for indexer in desired if indexer not in catalog]
    already = [
        indexer
        for indexer, details in catalog.items()
        if indexer in desired and details.configured and indexer not in enabled_set
    ]
    still_disabled = [
        indexer
        for indexer, details in catalog.items()
        if indexer in desired and not details.configured and indexer not in failed_set
    ]

    lines = ["Jackett indexer reconciliation", ""]
    append_indexer_group(lines, "Enabled", enabled, desired)
    append_indexer_group(lines, "Already configured", sorted(already), desired)
    append_indexer_group(lines, "Not found in Jackett", sorted(missing), desired)

    if failed:
        lines.append("Failed")
        for indexer, message in sorted(failed.items()):
            media = ", ".join(desired.get(indexer, ["?"]))
            lines.append(f"- {display_name(indexer)} ({media}): {message}")
        lines.append("")

    append_indexer_group(lines, "Still disabled", sorted(still_disabled), desired)
    return lines


def indexer_reconciliation_failure_count(
    desired: dict[str, list[str]],
    catalog: dict[str, JackettIndexer],
    failed: dict[str, str],
) -> int:
    count = len(failed)
    count += sum(1 for indexer in desired if indexer not in catalog)
    count += sum(
        1
        for indexer, details in catalog.items()
        if indexer in desired and not details.configured and indexer not in failed
    )
    return count


def append_indexer_group(
    lines: list[str],
    title: str,
    indexers: list[str],
    desired: dict[str, list[str]],
) -> None:
    if not indexers:
        return
    lines.append(title)
    for indexer in indexers:
        media = ", ".join(desired.get(indexer, ["?"]))
        lines.append(f"- {display_name(indexer)} ({media})")
    lines.append("")


def supports_media_categories(indexer: JackettIndexer, categories: tuple[str, ...]) -> bool:
    if not categories:
        return "search" in indexer.search_types
    if any(
        category_matches(selected, actual)
        for selected in categories
        for actual in indexer.categories
    ):
        return True
    search_type = search_type_for_categories(categories)
    return search_type in indexer.search_types


def search_type_for_categories(categories: tuple[str, ...]) -> str:
    for category in categories:
        try:
            family = int(category) // 1000
        except ValueError:
            continue
        if family == 2:
            return "movie-search"
        if family == 3:
            return "audio-search"
        if family == 5:
            return "tv-search"
        if family == 7:
            return "book-search"
    return "search"


def suggest_move_target(
    categories: tuple[str, ...],
    library_dir: Path,
    source_name: str,
    source_is_dir: bool,
) -> MoveSuggestion:
    fallback = MoveSuggestion(
        dest_dir=str(library_dir),
        filename=source_name,
        message="Automatic naming: could not identify this item. Using the current name.",
    )
    if not source_name:
        return fallback

    naming_rule = move_naming_rule(categories)
    if naming_rule is None:
        return MoveSuggestion(
            dest_dir=str(library_dir),
            filename=source_name,
            message="Automatic naming: no supported rule for this media category. Using the current name.",
        )

    try:
        from guessit import guessit
    except ImportError:
        return MoveSuggestion(
            dest_dir=str(library_dir),
            filename=source_name,
            message="Automatic naming unavailable: install dependencies from requirements.txt.",
        )

    try:
        info = dict(guessit(source_name))
    except Exception as error:
        return MoveSuggestion(
            dest_dir=str(library_dir),
            filename=source_name,
            message=f"Automatic naming failed: GuessIt could not parse this item ({error}).",
        )

    if naming_rule == "movie":
        return suggest_movie_target(library_dir, source_name, source_is_dir, info)
    if naming_rule == "show":
        return suggest_episode_target(library_dir, source_name, source_is_dir, info)

    return fallback


def infer_media_key(source_name: str, media_types: dict) -> str | None:
    if not source_name:
        return None

    try:
        from guessit import guessit
    except ImportError:
        return None

    try:
        info = dict(guessit(source_name))
    except Exception:
        return None

    if not _looks_like_show(info):
        return None

    for media_key, media in media_types.items():
        if move_naming_rule(media.categories) == "show":
            return media_key
    return None


def move_naming_rule(categories: tuple[str, ...]) -> str | None:
    search_type = search_type_for_categories(categories)
    if search_type == "movie-search":
        return "movie"
    if search_type == "tv-search":
        return "show"
    return None


def suggest_movie_target(
    library_dir: Path,
    source_name: str,
    source_is_dir: bool,
    info: dict,
) -> MoveSuggestion:
    title = clean_path_component(scalar_text(info.get("title")))
    if not title:
        return MoveSuggestion(
            dest_dir=str(library_dir),
            filename=source_name,
            message="Automatic naming: GuessIt did not find a movie title. Using the current name.",
        )

    year = scalar_text(info.get("year"))
    movie_name = f"{title} ({year})" if year else title
    message = "Automatic naming: suggested movie title/year from GuessIt."
    if not year:
        message = "Automatic naming: found a movie title, but no year. Review before moving."

    if source_is_dir:
        return MoveSuggestion(dest_dir=str(library_dir), filename=movie_name, message=message)

    extension = Path(source_name).suffix
    return MoveSuggestion(
        dest_dir=str(library_dir / movie_name),
        filename=f"{movie_name}{extension}",
        message=message,
    )


def suggest_episode_target(
    library_dir: Path,
    source_name: str,
    source_is_dir: bool,
    info: dict,
) -> MoveSuggestion:
    if source_is_dir:
        return MoveSuggestion(
            dest_dir=str(library_dir),
            filename=source_name,
            message="Automatic naming: episode renaming is only supported for single files.",
        )

    show = clean_path_component(scalar_text(info.get("title")))
    season = scalar_int(info.get("season"))
    episode = scalar_int(info.get("episode"))
    if not show or season is None or episode is None:
        return MoveSuggestion(
            dest_dir=str(library_dir),
            filename=source_name,
            message="Automatic naming: GuessIt could not find show, season, and episode.",
        )

    episode_title = clean_path_component(scalar_text(info.get("episode_title")))
    season_dir = f"Season {season:02d}"
    episode_name = f"{show} - S{season:02d}E{episode:02d}"
    if episode_title:
        episode_name = f"{episode_name} - {episode_title}"
    extension = Path(source_name).suffix
    return MoveSuggestion(
        dest_dir=str(library_dir / show / season_dir),
        filename=f"{episode_name}{extension}",
        message="Automatic naming: suggested episode path from GuessIt.",
    )


def scalar_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value).strip()


def scalar_int(value) -> int | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def clean_path_component(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned


def _looks_like_show(info: dict) -> bool:
    if scalar_int(info.get("season")) is not None:
        return True
    episode = info.get("episode")
    if isinstance(episode, (list, tuple)):
        return any(scalar_int(item) is not None for item in episode)
    return scalar_int(episode) is not None
