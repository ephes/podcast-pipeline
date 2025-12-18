from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace, ProvenanceRef, ReviewIteration, TextFormat


class WorkspaceStoreError(RuntimeError):
    pass


_PATH_SEGMENT_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe_path_segment(value: str) -> str:
    if "/" in value or "\\" in value:
        raise ValueError("path segment must not contain path separators")
    cleaned = _PATH_SEGMENT_RE.sub("_", value).strip("._-")
    if not cleaned:
        raise ValueError("path segment must not be empty after sanitization")
    return cleaned


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)

    try:
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise WorkspaceStoreError(f"Invalid JSON at {path}: {exc}") from exc


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(_read_text(path))
    except yaml.YAMLError as exc:
        raise WorkspaceStoreError(f"Invalid YAML at {path}: {exc}") from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise WorkspaceStoreError(f"Expected mapping YAML at {path}, got {type(loaded).__name__}")
    return dict(loaded)


def _format_to_extension(fmt: TextFormat) -> str:
    match fmt:
        case TextFormat.markdown:
            return "md"
        case TextFormat.html:
            return "html"
        case TextFormat.plain:
            return "txt"


@dataclass(frozen=True)
class EpisodeWorkspaceLayout:
    root: Path

    @property
    def episode_yaml(self) -> Path:
        return self.root / "episode.yaml"

    @property
    def state_json(self) -> Path:
        return self.root / "state.json"

    @property
    def copy_dir(self) -> Path:
        return self.root / "copy"

    @property
    def copy_candidates_dir(self) -> Path:
        return self.copy_dir / "candidates"

    @property
    def copy_reviews_dir(self) -> Path:
        return self.copy_dir / "reviews"

    @property
    def copy_selected_dir(self) -> Path:
        return self.copy_dir / "selected"

    @property
    def copy_provenance_dir(self) -> Path:
        return self.copy_dir / "provenance"

    @property
    def copy_protocol_dir(self) -> Path:
        return self.copy_dir / "protocol"

    def candidate_json_path(self, asset_id: str, candidate_id: UUID) -> Path:
        safe_asset = _safe_path_segment(asset_id)
        return self.copy_candidates_dir / safe_asset / f"candidate_{candidate_id}.json"

    def review_iteration_json_path(self, asset_id: str, iteration: int, *, reviewer: str | None = None) -> Path:
        safe_asset = _safe_path_segment(asset_id)
        suffix = ""
        if reviewer is not None:
            suffix = f".{_safe_path_segment(reviewer)}"
        return self.copy_reviews_dir / safe_asset / f"iteration_{iteration:02d}{suffix}.json"

    def protocol_state_json_path(self, asset_id: str) -> Path:
        safe_asset = _safe_path_segment(asset_id)
        return self.copy_protocol_dir / safe_asset / "state.json"

    def protocol_iteration_json_path(self, asset_id: str, iteration: int) -> Path:
        safe_asset = _safe_path_segment(asset_id)
        return self.copy_protocol_dir / safe_asset / f"iteration_{iteration:02d}.json"

    def selected_text_path(self, asset_id: str, fmt: TextFormat) -> Path:
        safe_asset = _safe_path_segment(asset_id)
        ext = _format_to_extension(fmt)
        return self.copy_selected_dir / f"{safe_asset}.{ext}"

    def provenance_json_path(self, kind: str, ref: str) -> Path:
        safe_kind = _safe_path_segment(kind)
        safe_ref = _safe_path_segment(ref)
        return self.copy_provenance_dir / safe_kind / f"{safe_ref}.json"


class EpisodeWorkspaceStore:
    def __init__(self, root: Path) -> None:
        self.layout = EpisodeWorkspaceLayout(root=root)

    def read_episode_yaml(self) -> dict[str, Any]:
        return _read_yaml_mapping(self.layout.episode_yaml)

    def write_episode_yaml(self, data: Mapping[str, Any]) -> None:
        dumped = yaml.safe_dump(dict(data), sort_keys=True)
        if not dumped.endswith("\n"):
            dumped += "\n"
        _atomic_write_text(self.layout.episode_yaml, dumped)

    def read_state(self) -> EpisodeWorkspace:
        raw = _read_text(self.layout.state_json)
        try:
            return EpisodeWorkspace.from_json(raw)
        except Exception as exc:
            raise WorkspaceStoreError(f"Invalid state.json at {self.layout.state_json}: {exc}") from exc

    def write_state(self, workspace: EpisodeWorkspace) -> None:
        payload = workspace.model_dump(mode="json")
        dumped = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        _atomic_write_text(self.layout.state_json, dumped)

    def write_candidate(self, candidate: Candidate) -> Path:
        path = self.layout.candidate_json_path(candidate.asset_id, candidate.candidate_id)
        dumped = json.dumps(candidate.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        _atomic_write_text(path, dumped)
        return path

    def read_candidate(self, asset_id: str, candidate_id: UUID) -> Candidate:
        path = self.layout.candidate_json_path(asset_id, candidate_id)
        raw = _read_json(path)
        return Candidate.model_validate(raw)

    def write_review(self, asset_id: str, review: ReviewIteration) -> Path:
        path = self.layout.review_iteration_json_path(asset_id, review.iteration, reviewer=review.reviewer)
        dumped = json.dumps(review.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        _atomic_write_text(path, dumped)
        return path

    def read_review(self, asset_id: str, iteration: int, *, reviewer: str | None = None) -> ReviewIteration:
        path = self.layout.review_iteration_json_path(asset_id, iteration, reviewer=reviewer)
        raw = _read_json(path)
        return ReviewIteration.model_validate(raw)

    def write_selected_text(self, asset_id: str, fmt: TextFormat, content: str) -> Path:
        path = self.layout.selected_text_path(asset_id, fmt)
        _atomic_write_text(path, content if content.endswith("\n") else content + "\n")
        return path

    def read_selected_text(self, asset_id: str, fmt: TextFormat) -> str:
        return _read_text(self.layout.selected_text_path(asset_id, fmt))

    def write_provenance_json(self, provenance: ProvenanceRef, data: Mapping[str, Any]) -> Path:
        ref = provenance.ref
        if not ref:
            raise ValueError("provenance.ref must be non-empty")
        path = self.layout.provenance_json_path(provenance.kind, ref)
        enriched: dict[str, Any] = dict(data)
        if provenance.created_at is not None:
            enriched.setdefault("created_at", _as_iso(provenance.created_at))
        dumped = json.dumps(enriched, indent=2, sort_keys=True) + "\n"
        _atomic_write_text(path, dumped)
        return path


def _as_iso(dt: datetime) -> str:
    return dt.isoformat()
