from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import typer

from podcast_pipeline.workspace_store import EpisodeWorkspaceStore, WorkspaceStoreError

_TRACK_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_TRACK_ID_CLEAN_RE = re.compile(r"[^a-z0-9]+")
_TRACK_PERSON_NUMBER_RE = re.compile(r"^(?P<name>.+?)[\s._-]*[\[(]?(?P<number>\d{1,3})[\])]?$")
_TRACK_LABEL_CLEAN_RE = re.compile(r"[\s._-]+")


def run_ingest(
    *,
    workspace: Path,
    reaper_media_dir: Path,
    tracks_glob: str,
) -> None:
    if not tracks_glob.strip():
        raise typer.BadParameter("tracks_glob must be non-empty")

    media_dir = reaper_media_dir.expanduser()
    if not media_dir.exists():
        raise typer.BadParameter(f"reaper media dir does not exist: {media_dir}")
    if not media_dir.is_dir():
        raise typer.BadParameter(f"reaper media dir is not a directory: {media_dir}")
    media_dir = media_dir.resolve()

    store = EpisodeWorkspaceStore(workspace)
    try:
        episode_yaml = store.read_episode_yaml()
    except FileNotFoundError as exc:
        raise typer.BadParameter(f"episode.yaml not found in {workspace}") from exc
    except WorkspaceStoreError as exc:
        raise typer.BadParameter(str(exc)) from exc

    sources = episode_yaml.get("sources")
    if not isinstance(sources, dict):
        sources = {}
    sources = dict(sources)
    sources["reaper_media_dir"] = str(media_dir)
    sources["tracks_glob"] = tracks_glob
    episode_yaml["sources"] = sources

    track_paths = _collect_track_paths(media_dir, tracks_glob)
    existing_tracks = _index_existing_tracks(episode_yaml.get("tracks"), media_dir)
    episode_yaml["tracks"] = _build_tracks(track_paths, existing_tracks, media_dir)

    store.write_episode_yaml(episode_yaml)
    typer.echo(f"Workspace: {workspace}")
    typer.echo(f"Tracks: {len(episode_yaml['tracks'])}")


def _collect_track_paths(media_dir: Path, tracks_glob: str) -> list[Path]:
    candidates = [path for path in media_dir.glob(tracks_glob) if path.is_file()]
    candidates.sort(key=lambda path: path.as_posix())
    return candidates


def _index_existing_tracks(raw_tracks: object, media_dir: Path) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_tracks, list):
        return {}

    media_dir = media_dir.resolve()
    mapping: dict[str, dict[str, Any]] = {}
    for entry in raw_tracks:
        if not isinstance(entry, dict):
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        raw_path = raw_path.strip()
        for key in _track_path_keys(raw_path, media_dir):
            mapping.setdefault(key, entry)
    return mapping


def _track_path_keys(raw_path: str, media_dir: Path) -> set[str]:
    keys = {_path_key(raw_path)}
    path_obj = Path(raw_path)
    if path_obj.is_absolute():
        rel = _safe_relative_path(path_obj, media_dir)
        if rel is not None:
            keys.add(rel.as_posix())
    else:
        abs_path = _safe_resolve_path(media_dir / path_obj)
        if abs_path is not None:
            keys.add(abs_path.as_posix())
    return keys


def _safe_relative_path(path: Path, base: Path) -> Path | None:
    try:
        return path.resolve().relative_to(base)
    except Exception:
        return None


def _safe_resolve_path(path: Path) -> Path | None:
    try:
        return path.resolve()
    except Exception:
        return None


def _build_tracks(
    track_paths: list[Path],
    existing_tracks: dict[str, dict[str, Any]],
    media_dir: Path,
) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for path in track_paths:
        rel_path = path.relative_to(media_dir).as_posix()
        abs_key = path.resolve().as_posix()
        existing = _find_existing_track(existing_tracks, rel_path, abs_key)
        track_id = _choose_track_id(existing, path, used_ids)
        label = _choose_label(existing, path)
        role = _choose_role(existing)
        track: dict[str, Any] = {"track_id": track_id, "path": rel_path}
        if label:
            track["label"] = label
        if role:
            track["role"] = role
        if existing:
            for key, value in existing.items():
                if key in {"track_id", "path", "label", "role"}:
                    continue
                track.setdefault(key, value)
        tracks.append(track)
    return tracks


def _find_existing_track(
    existing_tracks: dict[str, dict[str, Any]],
    rel_key: str,
    abs_key: str,
) -> dict[str, Any] | None:
    for key in (rel_key, abs_key, _path_key(rel_key), _path_key(abs_key)):
        existing = existing_tracks.get(key)
        if existing is not None:
            return existing
    return None


def _choose_track_id(
    existing: dict[str, Any] | None,
    path: Path,
    used_ids: set[str],
) -> str:
    candidate = None
    if existing is not None:
        raw = existing.get("track_id")
        if isinstance(raw, str) and raw.strip():
            candidate = _sanitize_track_id(raw)
    if candidate is None:
        parsed = _parse_person_number(path.stem)
        if parsed is not None:
            name, number = parsed
            base = _sanitize_track_id(name)
            candidate = f"{base}_{number:02d}"
        else:
            candidate = _sanitize_track_id(path.stem)
    candidate = _ensure_track_prefix(candidate)
    return _unique_track_id(candidate, used_ids)


def _choose_label(existing: dict[str, Any] | None, path: Path) -> str | None:
    if existing is not None:
        raw = existing.get("label")
        if isinstance(raw, str) and raw.strip():
            return raw
    parsed = _parse_person_number(path.stem)
    if parsed is not None:
        name, number = parsed
        base = _normalize_label(name)
        if base:
            return f"{base} {number}"
    label = _normalize_label(path.stem)
    return label or None


def _choose_role(existing: dict[str, Any] | None) -> str | None:
    if existing is not None:
        raw = existing.get("role")
        if isinstance(raw, str) and raw.strip():
            return raw
    return None


def _sanitize_track_id(value: str) -> str:
    cleaned = _TRACK_ID_CLEAN_RE.sub("_", value.strip().lower()).strip("_")
    return cleaned or "track"


def _ensure_track_prefix(value: str) -> str:
    if _TRACK_ID_RE.fullmatch(value):
        return value
    if value and value[0].isalpha():
        value = _TRACK_ID_CLEAN_RE.sub("_", value).strip("_")
        if _TRACK_ID_RE.fullmatch(value):
            return value
    return f"track_{value}" if value else "track"


def _unique_track_id(candidate: str, used_ids: set[str]) -> str:
    if candidate not in used_ids:
        used_ids.add(candidate)
        return candidate
    index = 2
    while True:
        deduped = f"{candidate}_{index:02d}"
        if deduped not in used_ids:
            used_ids.add(deduped)
            return deduped
        index += 1


def _path_key(value: str) -> str:
    return value.replace("\\", "/")


def _parse_person_number(stem: str) -> tuple[str, int] | None:
    candidate = stem.strip()
    if not candidate:
        return None
    match = _TRACK_PERSON_NUMBER_RE.match(candidate)
    if not match:
        return None
    name = match.group("name").strip(" _-")
    if not name or not re.search(r"[A-Za-z]", name):
        return None
    number = int(match.group("number"))
    return name, number


def _normalize_label(value: str) -> str:
    return _TRACK_LABEL_CLEAN_RE.sub(" ", value).strip()
