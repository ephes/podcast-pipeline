from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from podcast_pipeline.agent_cli_config import global_config_path
from podcast_pipeline.domain.models import TextFormat
from podcast_pipeline.tag_parsing import parse_tag_list
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


class AuphonicConfigError(RuntimeError):
    pass


_PREFERRED_TRACK_ROLES = {"mix", "master", "final", "mixdown"}


def build_auphonic_payload(
    *,
    episode_yaml: Mapping[str, Any],
    workspace: Path,
) -> dict[str, Any]:
    global_path = global_config_path()
    global_data = _load_yaml_mapping(global_path)
    global_section = _extract_auphonic_section(global_data, source=str(global_path))
    episode_section = _extract_auphonic_section(episode_yaml, source="episode.yaml")
    preset_map = _merge_preset_maps(
        _extract_presets(global_section, source=str(global_path)),
        _extract_presets(episode_section, source="episode.yaml"),
    )
    merged = dict(global_section)
    merged.update(episode_section)

    preset_id = _resolve_preset_id(merged, preset_map)
    metadata = _resolve_metadata(
        config=merged,
        episode_yaml=episode_yaml,
        workspace=workspace,
    )
    input_files = _resolve_input_files(
        config=merged,
        episode_yaml=episode_yaml,
        workspace=workspace,
    )
    chapters = _normalize_chapters(merged.get("chapters"), source="auphonic.chapters")

    payload: dict[str, Any] = {"preset": preset_id}
    title = metadata.get("title")
    if _has_value(title):
        payload["title"] = title
    if input_files:
        if len(input_files) == 1:
            payload["input_file"] = input_files[0]
        else:
            payload["input_files"] = input_files
    if metadata:
        payload["metadata"] = metadata
    if chapters:
        payload["chapters"] = chapters
    return payload


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AuphonicConfigError(f"Invalid YAML at {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise AuphonicConfigError(f"Expected mapping YAML at {path}")
    return dict(loaded)


def _extract_auphonic_section(data: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    raw = data.get("auphonic")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise AuphonicConfigError(f"Expected mapping for auphonic in {source}")
    return dict(raw)


def _extract_presets(section: Mapping[str, Any], *, source: str) -> dict[str, str]:
    raw = section.get("presets")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise AuphonicConfigError(f"Expected mapping for auphonic.presets in {source}")
    presets: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise AuphonicConfigError(f"auphonic.presets keys must be non-empty strings in {source}")
        if not isinstance(value, str) or not value.strip():
            raise AuphonicConfigError(f"auphonic.presets.{key} must be a non-empty string in {source}")
        presets[key] = value
    return presets


def _merge_preset_maps(global_presets: Mapping[str, str], episode_presets: Mapping[str, str]) -> dict[str, str]:
    merged = dict(global_presets)
    merged.update(episode_presets)
    return merged


def _resolve_preset_id(config: Mapping[str, Any], preset_map: Mapping[str, str]) -> str:
    preset_id = config.get("preset_id")
    if preset_id is not None:
        if not isinstance(preset_id, str) or not preset_id.strip():
            raise AuphonicConfigError("auphonic.preset_id must be a non-empty string")
        return preset_id.strip()

    preset = config.get("preset")
    if not isinstance(preset, str) or not preset.strip():
        raise AuphonicConfigError("Missing auphonic.preset (or preset_id) in episode.yaml or config.yaml")
    preset_key = preset.strip()
    return preset_map.get(preset_key, preset_key)


def _resolve_metadata(
    *,
    config: Mapping[str, Any],
    episode_yaml: Mapping[str, Any],
    workspace: Path,
) -> dict[str, Any]:
    metadata = _normalize_metadata(config.get("metadata"), source="auphonic.metadata")

    _set_default(metadata, "title", _optional_str(config.get("title"), key="auphonic.title"))
    _set_default(metadata, "subtitle", _optional_str(config.get("subtitle"), key="auphonic.subtitle"))
    _set_default(metadata, "summary", _optional_str(config.get("summary"), key="auphonic.summary"))
    _set_default(metadata, "description", _optional_str(config.get("description"), key="auphonic.description"))

    tags = _normalize_tags(config.get("tags"), source="auphonic.tags")
    if tags is not None:
        _set_default(metadata, "tags", tags)

    keywords = _normalize_keywords(config.get("itunes_keywords"), source="auphonic.itunes_keywords")
    if keywords is not None:
        _set_default(metadata, "itunes_keywords", keywords)

    selected = _load_selected_assets(workspace)
    _set_default(
        metadata,
        "title",
        _first_content_line(selected.get("title_detail")) or _first_content_line(selected.get("title_seo")),
    )
    _set_default(metadata, "subtitle", _first_content_line(selected.get("subtitle_auphonic")))
    _set_default(metadata, "summary", _first_content_line(selected.get("summary_short")))
    _set_default(metadata, "description", _strip_heading(selected.get("description")))

    selected_tags = parse_tag_list(selected.get("audio_tags")) or parse_tag_list(selected.get("cms_tags"))
    if selected_tags:
        _set_default(metadata, "tags", selected_tags)

    selected_keywords = parse_tag_list(selected.get("itunes_keywords"))
    if selected_keywords:
        _set_default(metadata, "itunes_keywords", ", ".join(selected_keywords))

    if not _has_value(metadata.get("title")):
        episode_id = episode_yaml.get("episode_id")
        if isinstance(episode_id, str) and episode_id.strip():
            metadata["title"] = episode_id.strip()

    for key in list(metadata.keys()):
        if not _has_value(metadata[key]):
            metadata.pop(key, None)
    return metadata


def _resolve_input_files(
    *,
    config: Mapping[str, Any],
    episode_yaml: Mapping[str, Any],
    workspace: Path,
) -> list[str]:
    input_files = _normalize_input_paths(
        config.get("input_files"),
        source="auphonic.input_files",
        workspace=workspace,
    )
    if input_files is None:
        input_files = _normalize_input_paths(
            config.get("input_file"),
            source="auphonic.input_file",
            workspace=workspace,
        )
    if input_files:
        return input_files

    return _resolve_track_inputs(episode_yaml=episode_yaml, workspace=workspace)


def _normalize_metadata(raw: object, *, source: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise AuphonicConfigError(f"Expected mapping for {source}")
    metadata = dict(raw)

    for key in ("title", "subtitle", "summary", "description"):
        if key in metadata:
            metadata[key] = _optional_str(metadata[key], key=f"{source}.{key}")

    if "tags" in metadata:
        metadata["tags"] = _normalize_tags(metadata.get("tags"), source=f"{source}.tags")
    if "itunes_keywords" in metadata:
        metadata["itunes_keywords"] = _normalize_keywords(
            metadata.get("itunes_keywords"),
            source=f"{source}.itunes_keywords",
        )
    return metadata


def _optional_str(value: object, *, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AuphonicConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _normalize_tags(value: object, *, source: str) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
        return items
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise AuphonicConfigError(f"{source} must be a list of non-empty strings")
        return [item.strip() for item in value]
    raise AuphonicConfigError(f"{source} must be a string or list of strings")


def _normalize_keywords(value: object, *, source: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise AuphonicConfigError(f"{source} must be a list of non-empty strings")
        return ", ".join(item.strip() for item in value)
    raise AuphonicConfigError(f"{source} must be a string or list of strings")


def _normalize_input_paths(
    value: object,
    *,
    source: str,
    workspace: Path,
) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [_resolve_path(value, workspace=workspace)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise AuphonicConfigError(f"{source} must be a list of non-empty strings")
        return [_resolve_path(item, workspace=workspace) for item in value]
    raise AuphonicConfigError(f"{source} must be a string or list of strings")


def _resolve_path(raw: str, *, workspace: Path) -> str:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return str(path)
    return str((workspace / path).resolve())


def _resolve_track_inputs(*, episode_yaml: Mapping[str, Any], workspace: Path) -> list[str]:
    tracks = episode_yaml.get("tracks")
    if not isinstance(tracks, list):
        return []
    base_dir = _resolve_reaper_media_dir(episode_yaml)
    preferred: list[str] = []
    fallback: list[str] = []
    for item in tracks:
        if not isinstance(item, Mapping):
            continue
        path_raw = item.get("path")
        if not isinstance(path_raw, str) or not path_raw.strip():
            continue
        resolved = _resolve_track_path(path_raw, base_dir=base_dir, workspace=workspace)
        role = item.get("role")
        if isinstance(role, str) and role.strip().lower() in _PREFERRED_TRACK_ROLES:
            preferred.append(resolved)
        else:
            fallback.append(resolved)

    if preferred:
        return preferred
    if len(fallback) == 1:
        return fallback
    return []


def _resolve_reaper_media_dir(episode_yaml: Mapping[str, Any]) -> Path | None:
    sources = episode_yaml.get("sources")
    if not isinstance(sources, Mapping):
        return None
    raw = sources.get("reaper_media_dir")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return None


def _resolve_track_path(raw: str, *, base_dir: Path | None, workspace: Path) -> str:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return str(path)
    if base_dir is not None:
        return str((base_dir / path).resolve())
    return str((workspace / path).resolve())


def _load_selected_assets(workspace: Path) -> dict[str, str]:
    layout = EpisodeWorkspaceLayout(root=workspace)
    assets = [
        "title_detail",
        "title_seo",
        "subtitle_auphonic",
        "summary_short",
        "description",
        "audio_tags",
        "cms_tags",
        "itunes_keywords",
    ]
    selected: dict[str, str] = {}
    for asset_id in assets:
        text = _read_selected_text(layout, asset_id)
        if text is not None:
            selected[asset_id] = text
    return selected


def _read_selected_text(layout: EpisodeWorkspaceLayout, asset_id: str) -> str | None:
    for fmt in (TextFormat.markdown, TextFormat.plain, TextFormat.html):
        path = layout.selected_text_path(asset_id, fmt)
        if path.exists():
            return path.read_text(encoding="utf-8")
    return None


def _first_content_line(text: str | None) -> str | None:
    if text is None:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped
    return None


def _strip_heading(text: str | None) -> str | None:
    if text is None:
        return None
    lines = text.splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines) and lines[idx].lstrip().startswith("#"):
        idx += 1
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
    body = "\n".join(lines[idx:]).strip()
    return body or None


def _normalize_chapters(value: object, *, source: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AuphonicConfigError(f"{source} must be a list")
    chapters: list[dict[str, Any]] = []
    for idx, item in enumerate(value, start=1):
        chapters.append(_normalize_chapter_item(item, source=source, idx=idx))
    return chapters


def _normalize_chapter_item(item: object, *, source: str, idx: int) -> dict[str, Any]:
    if isinstance(item, str):
        title = item.strip()
        if not title:
            raise AuphonicConfigError(f"{source}[{idx}] must be a non-empty string")
        return {"title": title}
    if not isinstance(item, Mapping):
        raise AuphonicConfigError(f"{source}[{idx}] must be a mapping or string")
    title_value = item.get("title")
    if not isinstance(title_value, str) or not title_value.strip():
        raise AuphonicConfigError(f"{source}[{idx}].title must be a non-empty string")
    entry: dict[str, Any] = {"title": title_value.strip()}
    start = _normalize_chapter_time(item, "start", source=source, idx=idx)
    if start is not None:
        entry["start"] = start
    end = _normalize_chapter_time(item, "end", source=source, idx=idx)
    if end is not None:
        entry["end"] = end
    return entry


def _normalize_chapter_time(
    item: Mapping[str, object],
    key: str,
    *,
    source: str,
    idx: int,
) -> float | None:
    value = item.get(key)
    if value is None:
        value = item.get(f"{key}_sec")
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise AuphonicConfigError(f"{source}[{idx}].{key} must be numeric") from exc
    raise AuphonicConfigError(f"{source}[{idx}].{key} must be numeric")


def _set_default(metadata: dict[str, Any], key: str, value: object) -> None:
    if _has_value(metadata.get(key)):
        return
    if _has_value(value):
        metadata[key] = value


def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True
