from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from pydantic import ValidationError

from podcast_pipeline.auphonic_history import record_new_production
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
    try:
        output_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        output_mode = None
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as tmp:
            tmp_path = Path(tmp.name)
            if output_mode is not None:
                os.fchmod(tmp.fileno(), output_mode)
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())

        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode())


def atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_text(path, text)


def _publish_markdown_selection(markdown_path: Path, html_path: Path, content: str) -> None:
    """Publish a Markdown selection and its derived HTML without losing the previous selection."""
    html = markdown_to_deterministic_html(content)  # rendered before anything is written
    if markdown_path.exists():
        # Markdown is already canonical, so the HTML is only derived: refresh it first
        # and replace the canonical file last.
        _atomic_write_text(html_path, html)
        _atomic_write_text(markdown_path, content)
        return
    # An existing HTML file may be the canonical selection. Create the Markdown first. If
    # the HTML then fails without having been replaced, remove the Markdown again so the
    # previous selection stays exactly as it was; if the HTML was replaced (the error came
    # after the rename), both new files stay.
    _atomic_write_text(markdown_path, content)
    try:
        _atomic_write_text(html_path, html)
    except BaseException:
        try:
            html_replaced = html_path.read_bytes() == html.encode()
        except FileNotFoundError:
            html_replaced = False
        except OSError:
            html_replaced = True  # unknown: keep the Markdown rather than delete
        if not html_replaced:
            with contextlib.suppress(OSError):
                markdown_path.unlink(missing_ok=True)
        raise


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

    @property
    def cms_dir(self) -> Path:
        return self.root / "cms"

    @property
    def cms_draft_json(self) -> Path:
        return self.cms_dir / "draft.json"

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

    def read_episode_yaml_with_revision(self) -> tuple[dict[str, Any], str]:
        with self._state_lock():
            data = self.read_episode_yaml()
            return data, self._episode_yaml_revision()

    def _episode_yaml_revision(self) -> str:
        return hashlib.sha256(self.layout.episode_yaml.read_bytes()).hexdigest()

    @contextmanager
    def update_episode_yaml(self, *, create_if_missing: bool = False) -> Iterator[dict[str, Any]]:
        """Lock, read the latest YAML, and publish only on successful exit."""
        with self._state_lock():
            try:
                data = self.read_episode_yaml()
            except FileNotFoundError:
                if not create_if_missing:
                    raise
                data = {}
            yield data
            self._write_episode_yaml_unlocked(data)

    def write_episode_yaml(
        self, data: Mapping[str, Any] | EpisodeYaml, *, expected_revision: str | None = None
    ) -> None:
        """Create YAML, or replace it only if the caller's revision still matches."""
        with self._state_lock():
            if self.layout.episode_yaml.exists():
                if expected_revision is None or expected_revision != self._episode_yaml_revision():
                    raise WorkspaceStoreError("episode.yaml changed; reload it before saving")
            elif expected_revision is not None:
                raise WorkspaceStoreError("episode.yaml disappeared; reload it before saving")
            self._write_episode_yaml_unlocked(data)

    def _write_episode_yaml_unlocked(self, data: Mapping[str, Any] | EpisodeYaml) -> None:
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

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        """Serialize read-modify-write cycles on ``state.json`` across threads and processes."""
        # Lock the workspace directory itself so no lock file shows up in the workspace.
        self.layout.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.layout.root, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the lock

    def _stored_auphonic_fields(self) -> dict[str, Any]:
        if not self.layout.state_json.exists():
            return {}
        try:
            stored = self.read_state()
        except WorkspaceStoreError:
            return {}
        if stored.auphonic_production_uuid is None and not stored.auphonic_productions:
            return {}
        # Both fields together, so the written state stays consistent.
        return {
            "auphonic_production_uuid": stored.auphonic_production_uuid,
            "auphonic_productions": stored.auphonic_productions,
        }

    def _write_state_unlocked(self, workspace: EpisodeWorkspace) -> None:
        payload = workspace.model_dump(mode="json")
        if payload.get("auphonic_production_uuid") is None:
            payload.pop("auphonic_production_uuid", None)
        if not payload.get("auphonic_productions"):
            payload.pop("auphonic_productions", None)
        dumped = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        _atomic_write_text(self.layout.state_json, dumped)

    def write_state(self, workspace: EpisodeWorkspace) -> None:
        """Write ``state.json``.

        A stored ``auphonic_production_uuid`` and production history
        (``auphonic_productions``) are never cleared or replaced here: writers
        holding an older in-memory copy (dashboard, pick UI, review loop) would
        otherwise drop or roll back the UUID of a paid production that
        ``podcast produce`` (or ``produce --restart``) saved in the meantime,
        or forget a superseded production that still has to be cleaned up.
        Only :meth:`set_auphonic_production_uuid` and
        :meth:`update_auphonic_productions` change them.
        """
        with self._state_lock():
            stored = self._stored_auphonic_fields()
            changed = {key: value for key, value in stored.items() if getattr(workspace, key) != value}
            if changed:
                workspace = workspace.model_copy(update=changed)
            self._write_state_unlocked(workspace)

    def set_auphonic_production_uuid(
        self,
        production_uuid: str,
        *,
        default: EpisodeWorkspace,
        superseded_reason: str | None = None,
    ) -> EpisodeWorkspace:
        """Record the new production ``production_uuid`` as the current one.

        Works on the current on-disk state (``default`` when none exists). The
        previously stored production stays in the history as superseded, with
        ``superseded_reason``.
        """
        return self.update_auphonic_productions(
            lambda workspace: record_new_production(workspace, production_uuid, reason=superseded_reason),
            default=default,
        )

    def update_auphonic_productions(
        self,
        update: Callable[[EpisodeWorkspace], EpisodeWorkspace],
        *,
        default: EpisodeWorkspace,
    ) -> EpisodeWorkspace:
        """Apply ``update`` to the on-disk state (``default`` when none exists) under the state lock."""
        with self._state_lock():
            current = self.read_state() if self.layout.state_json.exists() else default
            updated = EpisodeWorkspace.model_validate(update(current).model_dump())
            self._write_state_unlocked(updated)
            return updated

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
        published = {path}
        if fmt == TextFormat.markdown:
            html_path = self.layout.selected_text_path(asset_id, TextFormat.html)
            published.add(html_path)
            _publish_markdown_selection(path, html_path, content)
        else:
            _atomic_write_text(path, content)
        # Only once the new selection is published: drop files of superseded formats.
        for superseded in TextFormat:
            stale = self.layout.selected_text_path(asset_id, superseded)
            if stale in published:
                continue
            try:
                stale.unlink(missing_ok=True)
            except OSError as exc:
                raise WorkspaceStoreError(f"Failed to remove superseded selected text at {stale}: {exc}") from exc
        return path

    def clear_selected_text(self, asset_id: str) -> None:
        for fmt in TextFormat:
            path = self.layout.selected_text_path(asset_id, fmt)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise WorkspaceStoreError(f"Failed to remove selected text at {path}: {exc}") from exc

    def delete_candidate_files(self, asset_id: str, candidate_id: UUID) -> None:
        paths = {self.layout.candidate_json_path(asset_id, candidate_id)}
        for fmt in TextFormat:
            paths.add(self.layout.candidate_text_path(asset_id, candidate_id, fmt))
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise WorkspaceStoreError(f"Failed to remove candidate artifact at {path}: {exc}") from exc

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
