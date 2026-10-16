from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from podcast_pipeline.agent_cli_config import global_config_path
from podcast_pipeline.domain.models import TextFormat
from podcast_pipeline.tag_parsing import parse_tag_list
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


class AuphonicConfigError(RuntimeError):
    pass


# Track roles that mark a pre-mixed master. Such a file may feed transcription,
# but it is never an Auphonic input: Auphonic mixes the per-speaker stems itself.
MIX_TRACK_ROLES = frozenset({"mix", "master", "final", "mixdown"})

PRODUCTION_MODE_MULTITRACK = "multitrack"
PRODUCTION_MODE_SINGLE_TRACK = "single_track"
_PRODUCTION_MODES = (PRODUCTION_MODE_MULTITRACK, PRODUCTION_MODE_SINGLE_TRACK)


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
    chapters = _normalize_chapters(merged.get("chapters"), source="auphonic.chapters")
    inputs: dict[str, Any]
    if _resolve_mode(merged) == PRODUCTION_MODE_MULTITRACK:
        inputs = {
            "is_multitrack": True,
            "multi_input_files": _resolve_multitrack_inputs(
                config=merged,
                episode_yaml=episode_yaml,
                workspace=workspace,
            ),
        }
    else:
        inputs = {"input_file": _resolve_single_input_file(config=merged, workspace=workspace)}

    payload: dict[str, Any] = {"preset": preset_id}
    title = metadata.get("title")
    if _has_value(title):
        payload["title"] = title
    payload.update(inputs)
    output_basename = _resolve_output_basename(merged, episode_yaml)
    if output_basename is not None:
        payload["output_basename"] = output_basename
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


def _resolve_output_basename(config: Mapping[str, Any], episode_yaml: Mapping[str, Any]) -> str | None:
    """Name the output files after the episode; Auphonic otherwise names them after the first input track."""
    raw = config.get("output_basename")
    source = "auphonic.output_basename"
    if raw is None:
        raw = episode_yaml.get("episode_id")
        source = "episode_id"
        if not isinstance(raw, str) or not raw.strip():
            return None
    if not isinstance(raw, str) or not raw.strip():
        raise AuphonicConfigError(f"{source} must be a non-empty string")
    basename = raw.strip()
    if "/" in basename or "\\" in basename or basename in {".", ".."}:
        raise AuphonicConfigError(f"{source} must be a file name without directories, got {basename!r}")
    return basename


def _resolve_mode(config: Mapping[str, Any]) -> str:
    raw = config.get("mode")
    if raw is None:
        return PRODUCTION_MODE_MULTITRACK
    if not isinstance(raw, str) or raw.strip() not in _PRODUCTION_MODES:
        raise AuphonicConfigError(f"auphonic.mode must be one of: {', '.join(_PRODUCTION_MODES)}")
    return raw.strip()


def _resolve_single_input_file(*, config: Mapping[str, Any], workspace: Path) -> str:
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
    if not input_files:
        raise AuphonicConfigError("auphonic.mode single_track needs auphonic.input_file (one local path or URL)")
    if len(input_files) > 1:
        raise AuphonicConfigError(
            "auphonic.mode single_track takes exactly one input file, but "
            f"{len(input_files)} were configured ({', '.join(input_files)}). "
            "List per-speaker stems in auphonic.tracks for a multitrack production instead."
        )
    return input_files[0]


def _resolve_multitrack_inputs(
    *,
    config: Mapping[str, Any],
    episode_yaml: Mapping[str, Any],
    workspace: Path,
) -> list[dict[str, Any]]:
    for key in ("input_file", "input_files"):
        if config.get(key) is not None:
            raise AuphonicConfigError(
                f"auphonic.{key} is set, but Auphonic productions are multitrack: Auphonic receives the "
                "per-speaker stems and mixes them itself, never a pre-mixed master. List the exported "
                "stems in auphonic.tracks (or ingest them as tracks), and move a master mix used for "
                "transcription to inputs.transcription_audio. Set auphonic.mode: single_track only to "
                "upload one file on purpose."
            )

    if config.get("tracks") is not None:
        entries = _normalize_stem_entries(config.get("tracks"), workspace=workspace)
        source = "auphonic.tracks"
    else:
        entries = _stems_from_episode_tracks(episode_yaml=episode_yaml, workspace=workspace)
        source = "tracks"
    if not entries:
        raise AuphonicConfigError(
            "No speaker stems for the Auphonic multitrack production. List the exported per-speaker "
            "stems in auphonic.tracks, or ingest them as episode tracks (tracks with a mix/master/final/"
            "mixdown role are master mixes and are never sent to Auphonic)."
        )
    seen: set[str] = set()
    for entry in entries:
        if entry["id"] in seen:
            raise AuphonicConfigError(f"Duplicate Auphonic track id {entry['id']!r} in {source}")
        seen.add(entry["id"])
    return entries


def _normalize_stem_entries(raw: object, *, workspace: Path) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise AuphonicConfigError("auphonic.tracks must be a list of mappings with id and path")
    entries: list[dict[str, Any]] = []
    for idx, item in enumerate(raw, start=1):
        source = f"auphonic.tracks[{idx}]"
        if not isinstance(item, Mapping):
            raise AuphonicConfigError(f"{source} must be a mapping with id and path")
        track_id = _required_track_str(item.get("id"), key=f"{source}.id")
        path = _required_track_str(item.get("path"), key=f"{source}.path")
        entry = _multitrack_entry(track_id, _resolve_path(path, workspace=workspace))
        algorithms = item.get("algorithms")
        if algorithms is not None:
            if not isinstance(algorithms, Mapping):
                raise AuphonicConfigError(f"{source}.algorithms must be a mapping")
            entry["algorithms"] = dict(algorithms)
        offset = item.get("offset")
        if offset is not None:
            entry["offset"] = _normalize_offset(offset, source=f"{source}.offset")
        entries.append(entry)
    return entries


def _stems_from_episode_tracks(*, episode_yaml: Mapping[str, Any], workspace: Path) -> list[dict[str, Any]]:
    tracks = episode_yaml.get("tracks")
    if not isinstance(tracks, list):
        return []
    base_dir = _resolve_reaper_media_dir(episode_yaml)
    entries: list[dict[str, Any]] = []
    for idx, item in enumerate(tracks, start=1):
        if not isinstance(item, Mapping):
            continue
        path_raw = item.get("path")
        if not isinstance(path_raw, str) or not path_raw.strip():
            continue
        role = item.get("role")
        if isinstance(role, str) and role.strip().lower() in MIX_TRACK_ROLES:
            continue
        track_id = _required_track_str(item.get("track_id"), key=f"tracks[{idx}].track_id")
        entries.append(
            _multitrack_entry(track_id, _resolve_track_path(path_raw, base_dir=base_dir, workspace=workspace))
        )
    return entries


def _multitrack_entry(track_id: str, input_file: str) -> dict[str, Any]:
    return {"type": "multitrack", "id": track_id, "input_file": input_file}


def _required_track_str(value: object, *, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuphonicConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _normalize_offset(value: object, *, source: str) -> float:
    message = f"{source} must be finite nonnegative seconds (not boolean)"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuphonicConfigError(message)
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise AuphonicConfigError(message)
    return seconds


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
    parsed = urlparse(raw.strip())
    if parsed.scheme and parsed.netloc:
        return raw.strip()
    path = Path(raw).expanduser()
    if path.is_absolute():
        return str(path)
    return str((workspace / path).resolve())


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
        entry["start"] = _format_chapter_time(start)
    end = _normalize_chapter_time(item, "end", source=source, idx=idx)
    if end is not None:
        entry["end"] = _format_chapter_time(end)
    if start is not None and end is not None and end < start:
        raise AuphonicConfigError(f"{source}[{idx}].end must be >= start")
    return entry


def _format_chapter_time(seconds: float) -> str:
    """Auphonic takes chapter times as ``HH:MM:SS.mmm`` strings; numeric seconds are rejected."""
    total_ms = round(seconds * 1000)
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


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
    message = f"{source}[{idx}].{key} must be finite nonnegative seconds (not boolean)"
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise AuphonicConfigError(message)
    try:
        seconds = float(value)
    except (ValueError, OverflowError) as exc:
        raise AuphonicConfigError(message) from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise AuphonicConfigError(message)
    return seconds


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
