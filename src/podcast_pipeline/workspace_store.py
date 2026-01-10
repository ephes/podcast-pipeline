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
from pydantic import ValidationError

from podcast_pipeline.domain.episode_yaml import EpisodeYaml, try_load_episode_yaml
from podcast_pipeline.domain.models import (
    Candidate,
    EpisodeWorkspace,
    ProvenanceRef,
    ReviewIteration,
    TextFormat,
)
from podcast_pipeline.markdown_html import markdown_to_deterministic_html


class WorkspaceStoreError(RuntimeError):
    pass


_PATH_SEGMENT_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_EPISODES_DIRNAME = "episodes"


def _safe_path_segment(value: str) -> str:
    if "/" in value or "\\" in value:
        raise ValueError("path segment must not contain path separators")
    cleaned = _PATH_SEGMENT_RE.sub("_", value).strip("._-")
    if not cleaned:
        raise ValueError("path segment must not be empty after sanitization")
    return cleaned


def episodes_dir(project_root: Path) -> Path:
    return project_root / _EPISODES_DIRNAME


def episode_workspace_dir(project_root: Path, episode_id: str) -> Path:
    return episodes_dir(project_root) / _safe_path_segment(episode_id)


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
    _atomic_write_bytes(path, text.encode())


def atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_text(path, text)


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
    def transcript_dir(self) -> Path:
        return self.root / "transcript"

    @property
    def transcript_chunks_dir(self) -> Path:
        return self.transcript_dir / "chunks"

    @property
    def summaries_dir(self) -> Path:
        return self.root / "summaries"

    @property
    def chunk_summaries_dir(self) -> Path:
        return self.summaries_dir / "chunks"

    @property
    def episode_summary_dir(self) -> Path:
        return self.summaries_dir / "episode"

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

    @property
    def auphonic_dir(self) -> Path:
        return self.root / "auphonic"

    @property
    def auphonic_downloads_dir(self) -> Path:
        return self.auphonic_dir / "downloads"

    @property
    def auphonic_outputs_dir(self) -> Path:
        return self.auphonic_dir / "outputs"

    def candidate_json_path(self, asset_id: str, candidate_id: UUID) -> Path:
        safe_asset = _safe_path_segment(asset_id)
        return self.copy_candidates_dir / safe_asset / f"candidate_{candidate_id}.json"

    def candidate_text_path(
        self,
        asset_id: str,
        candidate_id: UUID,
        fmt: TextFormat,
    ) -> Path:
        safe_asset = _safe_path_segment(asset_id)
        ext = _format_to_extension(fmt)
        return self.copy_candidates_dir / safe_asset / f"candidate_{candidate_id}.{ext}"

    def review_iteration_json_path(
        self,
        asset_id: str,
        iteration: int,
        *,
        reviewer: str | None = None,
    ) -> Path:
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

    def creator_iteration_json_path(self, asset_id: str, iteration: int) -> Path:
        safe_asset = _safe_path_segment(asset_id)
        return self.copy_protocol_dir / safe_asset / f"iteration_{iteration:02d}.creator.json"

    def selected_text_path(self, asset_id: str, fmt: TextFormat) -> Path:
        safe_asset = _safe_path_segment(asset_id)
        ext = _format_to_extension(fmt)
        return self.copy_selected_dir / f"{safe_asset}.{ext}"

    def provenance_json_path(self, kind: str, ref: str) -> Path:
        safe_kind = _safe_path_segment(kind)
        safe_ref = _safe_path_segment(ref)
        return self.copy_provenance_dir / safe_kind / f"{safe_ref}.json"

    def transcript_chunk_text_path(self, chunk_id: int) -> Path:
        if chunk_id < 1:
            raise ValueError("chunk_id must be >= 1")
        return self.transcript_chunks_dir / f"chunk_{chunk_id:04d}.txt"

    def transcript_chunk_meta_json_path(self, chunk_id: int) -> Path:
        if chunk_id < 1:
            raise ValueError("chunk_id must be >= 1")
        return self.transcript_chunks_dir / f"chunk_{chunk_id:04d}.json"

    def chunk_summary_json_path(self, chunk_id: int) -> Path:
        if chunk_id < 1:
            raise ValueError("chunk_id must be >= 1")
        return self.chunk_summaries_dir / f"chunk_{chunk_id:04d}.summary.json"

    def episode_summary_json_path(self) -> Path:
        return self.episode_summary_dir / "episode_summary.json"

    def episode_summary_markdown_path(self) -> Path:
        return self.episode_summary_dir / "episode_summary.md"

    def episode_summary_html_path(self) -> Path:
        return self.episode_summary_dir / "episode_summary.html"


class EpisodeWorkspaceStore:
    def __init__(self, root: Path) -> None:
        self.layout = EpisodeWorkspaceLayout(root=root)

    def read_episode_yaml(self) -> dict[str, Any]:
        raw = _read_yaml_mapping(self.layout.episode_yaml)
        result = try_load_episode_yaml(raw)
        if result.error is not None:
            raise WorkspaceStoreError(f"Invalid episode.yaml at {self.layout.episode_yaml}: {result.error}")
        episode = result.value
        assert episode is not None
        return episode.to_mapping(exclude_unset=True)

    def write_episode_yaml(self, data: Mapping[str, Any] | EpisodeYaml) -> None:
        episode = data if isinstance(data, EpisodeYaml) else EpisodeYaml.model_validate(dict(data))
        dumped = yaml.safe_dump(episode.to_mapping(exclude_unset=True), sort_keys=True)
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
        if payload.get("auphonic_production_uuid") is None:
            payload.pop("auphonic_production_uuid", None)
        dumped = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        _atomic_write_text(self.layout.state_json, dumped)

    def write_candidate(self, candidate: Candidate) -> Path:
        path = self.layout.candidate_json_path(
            candidate.asset_id,
            candidate.candidate_id,
        )
        dumped = json.dumps(candidate.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        _atomic_write_text(path, dumped)
        text_path = self.layout.candidate_text_path(candidate.asset_id, candidate.candidate_id, candidate.format)
        content = candidate.content
        if not content.endswith("\n"):
            content += "\n"
        _atomic_write_text(text_path, content)
        if candidate.format == TextFormat.markdown:
            html_path = self.layout.candidate_text_path(candidate.asset_id, candidate.candidate_id, TextFormat.html)
            _atomic_write_text(html_path, markdown_to_deterministic_html(content))
        return path

    def read_candidate(self, asset_id: str, candidate_id: UUID) -> Candidate:
        path = self.layout.candidate_json_path(asset_id, candidate_id)
        raw = _read_json(path)
        try:
            return Candidate.model_validate(raw)
        except ValidationError as exc:
            raise WorkspaceStoreError(f"Invalid candidate JSON at {path}: {exc}") from exc

    def write_review(self, asset_id: str, review: ReviewIteration) -> Path:
        path = self.layout.review_iteration_json_path(
            asset_id,
            review.iteration,
            reviewer=review.reviewer,
        )
        dumped = json.dumps(review.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        _atomic_write_text(path, dumped)
        return path

    def read_review(
        self,
        asset_id: str,
        iteration: int,
        *,
        reviewer: str | None = None,
    ) -> ReviewIteration:
        path = self.layout.review_iteration_json_path(
            asset_id,
            iteration,
            reviewer=reviewer,
        )
        raw = _read_json(path)
        try:
            return ReviewIteration.model_validate(raw)
        except ValidationError as exc:
            raise WorkspaceStoreError(f"Invalid review JSON at {path}: {exc}") from exc

    def write_selected_text(
        self,
        asset_id: str,
        fmt: TextFormat,
        content: str,
    ) -> Path:
        path = self.layout.selected_text_path(asset_id, fmt)
        if not content.endswith("\n"):
            content += "\n"
        _atomic_write_text(path, content)
        if fmt == TextFormat.markdown:
            html_path = self.layout.selected_text_path(asset_id, TextFormat.html)
            _atomic_write_text(html_path, markdown_to_deterministic_html(content))
        return path

    def read_selected_text(self, asset_id: str, fmt: TextFormat) -> str:
        return _read_text(self.layout.selected_text_path(asset_id, fmt))

    def write_provenance_json(
        self,
        provenance: ProvenanceRef,
        data: Mapping[str, Any],
    ) -> Path:
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
