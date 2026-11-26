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
from typing import Any, BinaryIO
from uuid import UUID

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, Node, SequenceNode

from podcast_pipeline.auphonic_history import record_new_production
from podcast_pipeline.domain.episode_yaml import EpisodeYaml, try_load_episode_yaml
from podcast_pipeline.domain.models import (
    Candidate,
    EpisodeWorkspace,
    ProvenanceRef,
    ReviewIteration,
    TextFormat,
)
from podcast_pipeline.file_inputs import read_regular_utf8
from podcast_pipeline.markdown_html import markdown_to_deterministic_html
from podcast_pipeline.yaml_inputs import DuplicateYamlKeyError, UniqueKeySafeLoader


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


def _fsync_dir(path: Path, *, required: bool = False) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        if required:
            raise
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
            prefix=".write-",
            suffix=".tmp",
        ) as tmp:
            tmp_path = Path(tmp.name)
            if output_mode is not None:
                os.fchmod(tmp.fileno(), output_mode)
            written = tmp.write(data)
            if written != len(data):
                raise OSError(f"Short write staging {path}: accepted {written} of {len(data)} bytes")
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


def _stage_file_copy(source: BinaryIO, *, directory: Path, prefix: str) -> Path:
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=prefix, delete=False) as target:
            staged = Path(target.name)
            while block := source.read(64 * 1024):
                written = target.write(block)
                if written != len(block):
                    raise OSError(f"Short write staging {staged}: accepted {written} of {len(block)} bytes")
            target.flush()
            os.fchmod(target.fileno(), stat.S_IMODE(os.fstat(source.fileno()).st_mode))
            os.fsync(target.fileno())
        return staged
    except BaseException:
        if staged is not None:
            with contextlib.suppress(OSError):
                staged.unlink(missing_ok=True)
        raise


def _restore_preserved_files(backups: dict[Path, Path | None], retained: set[Path]) -> list[str]:
    failures: list[str] = []
    for path, saved in reversed(list(backups.items())):
        staged = None
        try:
            if saved is None:
                path.unlink(missing_ok=True)
            else:
                with saved.open("rb") as source:
                    staged = _stage_file_copy(source, directory=path.parent, prefix=".restore-")
                os.replace(staged, path)
            _fsync_dir(path.parent, required=True)
            if saved is not None:
                retained.discard(saved)
        except OSError as exc:
            recovery = f"; retained backup: {saved}" if saved is not None else ""
            failures.append(f"{path}: {exc}{recovery}")
        finally:
            if staged is not None:
                with contextlib.suppress(OSError):
                    staged.unlink(missing_ok=True)
    return failures


@contextmanager
def preserve_files(paths: list[Path]) -> Iterator[None]:
    """Restore named files after a handled failure; callers own serialization.

    Sibling backups survive until a handled restore is synced. Process death
    during publication can still leave a mixed set and backups.
    """
    backups: dict[Path, Path | None] = {}
    retained: set[Path] = set()
    try:
        for path in dict.fromkeys(paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                source = path.open("rb")
            except FileNotFoundError:
                backups[path] = None
            else:
                with source:
                    backups[path] = _stage_file_copy(source, directory=path.parent, prefix=".backup-")
        for directory in {path.parent for path, saved in backups.items() if saved is not None}:
            _fsync_dir(directory, required=True)
        try:
            yield
        except BaseException as original:
            retained.update(saved for saved in backups.values() if saved is not None)
            failures = _restore_preserved_files(backups, retained)
            if failures:
                message = "Artifact recovery failed (restore durability unconfirmed): " + "; ".join(failures)
                original.add_note(message)
                if isinstance(original, Exception):
                    raise WorkspaceStoreError(message) from original
            raise
    finally:
        for saved in backups.values():
            if saved is not None and saved not in retained:
                saved.unlink(missing_ok=True)


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


_EPISODE_YAML_MAX_NODES = 100_000
_EPISODE_YAML_MAX_DEPTH = 64


class _EpisodeYamlLoader(UniqueKeySafeLoader):
    def get_single_node(self) -> Node | None:
        try:
            root = super().get_single_node()
        except RecursionError:
            raise yaml.YAMLError("YAML depth limit exceeded") from None
        if root is not None:
            _admit_episode_yaml_graph(root)
        return root


def _admit_episode_yaml_graph(root: Node) -> None:
    # Count every use of an alias before SafeLoader can expand merge keys.
    occurrences = 0
    active: set[Node] = set()

    def visit(node: Node, depth: int) -> None:
        nonlocal occurrences
        occurrences += 1
        if occurrences > _EPISODE_YAML_MAX_NODES:
            raise yaml.YAMLError("YAML node limit exceeded")
        if depth > _EPISODE_YAML_MAX_DEPTH:
            raise yaml.YAMLError("YAML depth limit exceeded")
        if node in active:
            raise yaml.YAMLError("YAML alias cycle")
        if isinstance(node, (MappingNode, SequenceNode)):
            active.add(node)
            if isinstance(node, MappingNode):
                for key, value in node.value:
                    visit(key, depth + 1)
                    visit(value, depth + 1)
            else:
                for item in node.value:
                    visit(item, depth + 1)
            active.remove(node)

    visit(root, 1)


def read_optional_yaml_mapping(path: Path, *, max_bytes: int) -> dict[str, Any]:
    """Read an optional YAML mapping from one bounded regular file generation."""
    try:
        text = read_regular_utf8(path, max_bytes=max_bytes)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise WorkspaceStoreError(f"Could not read YAML at {path}: {exc}") from exc
    return _parse_yaml_mapping(text, path)


def _parse_yaml_mapping(text: str, path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.load(text, Loader=_EpisodeYamlLoader)
    except DuplicateYamlKeyError as exc:
        mark = exc.problem_mark
        assert mark is not None
        raise WorkspaceStoreError(
            f"Duplicate YAML key at {path}, line {mark.line + 1}, column {mark.column + 1}"
        ) from None
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

    def read_episode_yaml(self, *, max_bytes: int | None = None) -> dict[str, Any]:
        path = self.layout.episode_yaml
        text = _read_text(path) if max_bytes is None else read_regular_utf8(path, max_bytes=max_bytes)
        raw = _parse_yaml_mapping(text, path)
        result = try_load_episode_yaml(raw)
        if result.error is not None:
            raise WorkspaceStoreError(f"Invalid episode.yaml at {self.layout.episode_yaml}: {result.error}")
        episode = result.value
        assert episode is not None
        return episode.to_mapping(exclude_unset=True)

    def read_episode_hosts(self) -> list[str] | None:
        """Read the workspace's named hosts for summary prompts."""
        try:
            raw = self.read_episode_yaml().get("hosts")
        except FileNotFoundError:
            return None
        if isinstance(raw, list) and raw and all(isinstance(host, str) for host in raw):
            return raw
        return None

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
    def publication_lock(self) -> Iterator[None]:
        """Hold the stable workspace lock for artifact publication or discovery.

        Do not reacquire it or call state/YAML writers inside this scope.
        """
        with self._state_lock():
            yield

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
        try:
            stored = self.read_state()
        except FileNotFoundError:
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

    def update_state(
        self,
        update: Callable[[EpisodeWorkspace], EpisodeWorkspace],
        *,
        default: EpisodeWorkspace,
        artifact_paths: list[Path] | None = None,
    ) -> EpisodeWorkspace:
        """Reload and update state under its stable lock; callbacks must not reacquire it.

        Selection callbacks publish their artifacts here as well, serializing metadata
        and output with other selection writers. Same-asset changes are last-lock-writer wins.
        With artifact_paths, handled callback/state-write errors restore those files and
        state together. Callbacks must validate intended metadata before publishing.
        """
        with self._state_lock():
            current = self.read_state() if self.layout.state_json.exists() else default
            # Back up affected outputs under the same lock as the current state.
            with (
                preserve_files([self.layout.state_json, *artifact_paths])
                if artifact_paths
                else contextlib.nullcontext()
            ):
                updated = EpisodeWorkspace.model_validate(update(current).model_dump())
                self._write_state_unlocked(updated)
                return updated

    @contextmanager
    def read_snapshot(self, *, default: EpisodeWorkspace) -> Iterator[EpisodeWorkspace]:
        """Hold the publication lock across current metadata and artifact reads.

        Readers must not call a writer or reacquire this lock inside the scope.
        """
        with self._state_lock():
            yield self.read_state() if self.layout.state_json.exists() else default

    def read_selection_snapshot(
        self, asset_id: str, *, default: EpisodeWorkspace
    ) -> tuple[EpisodeWorkspace, dict[str, str]]:
        """Read selection metadata and all published formats under the writer lock."""
        with self._state_lock():
            current = self.read_state() if self.layout.state_json.exists() else default
            texts = {
                fmt.value: path.read_text(encoding="utf-8")
                for fmt in TextFormat
                if (path := self.layout.selected_text_path(asset_id, fmt)).exists()
            }
            return current, texts

    def write_candidate(self, candidate: Candidate, *, immutable: bool = False) -> Path:
        path = self.layout.candidate_json_path(
            candidate.asset_id,
            candidate.candidate_id,
        )
        dumped = json.dumps(candidate.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        text_path = self.layout.candidate_text_path(candidate.asset_id, candidate.candidate_id, candidate.format)
        content = candidate.content
        if not content.endswith("\n"):
            content += "\n"
        artifacts = {text_path: content}
        if candidate.format == TextFormat.markdown:
            html_path = self.layout.candidate_text_path(candidate.asset_id, candidate.candidate_id, TextFormat.html)
            artifacts[html_path] = markdown_to_deterministic_html(content)
        with self._state_lock():
            if immutable and path.exists():
                if self.read_candidate(candidate.asset_id, candidate.candidate_id) != candidate:
                    raise ValueError("Candidate identity cannot change; use a fresh candidate UUID.")
                missing: dict[Path, str] = {}
                for artifact_path, text in artifacts.items():
                    try:
                        mode = artifact_path.lstat().st_mode
                    except FileNotFoundError:
                        missing[artifact_path] = text
                        continue
                    if stat.S_ISREG(mode):
                        expected = text.encode()
                        with artifact_path.open("rb") as source:
                            if source.read(len(expected) + 1) == expected:
                                continue
                    raise WorkspaceStoreError(f"Immutable candidate companion conflicts at {artifact_path}")
                artifacts = missing
            else:
                # Publish JSON last: it makes candidates discoverable.
                artifacts[path] = dumped
            with preserve_files(list(artifacts)):
                for artifact_path, text in artifacts.items():
                    _atomic_write_text(artifact_path, text)
        return path

    def read_candidate(self, asset_id: str, candidate_id: UUID) -> Candidate:
        path = self.layout.candidate_json_path(asset_id, candidate_id)
        raw = _read_json(path)
        try:
            candidate = Candidate.model_validate(raw)
        except ValidationError as exc:
            raise WorkspaceStoreError(f"Invalid candidate JSON at {path}: {exc}") from exc
        if candidate.asset_id != asset_id or candidate.candidate_id != candidate_id:
            raise WorkspaceStoreError(f"Candidate identity does not match requested path at {path}")
        return candidate

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
            review = ReviewIteration.model_validate(raw)
        except ValidationError as exc:
            raise WorkspaceStoreError(f"Invalid review JSON at {path}: {exc}") from exc
        if review.iteration != iteration or review.reviewer != reviewer:
            raise WorkspaceStoreError(f"Review identity does not match requested path at {path}")
        return review

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
        self._remove_selected_text(asset_id, keep=published)
        return path

    def clear_selected_text(self, asset_id: str) -> None:
        self._remove_selected_text(asset_id, keep=set())

    def _remove_selected_text(self, asset_id: str, *, keep: set[Path]) -> None:
        removed = False
        for fmt in TextFormat:
            path = self.layout.selected_text_path(asset_id, fmt)
            if path in keep:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise WorkspaceStoreError(f"Failed to remove selected text at {path}: {exc}") from exc
            removed = True
        if removed:
            directory = self.layout.copy_selected_dir
            try:
                _fsync_dir(directory, required=True)
            except OSError as exc:
                raise WorkspaceStoreError(f"Selection deletion durability unconfirmed at {directory}: {exc}") from exc

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
