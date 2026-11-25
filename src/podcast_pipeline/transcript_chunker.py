from __future__ import annotations

import bisect
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from podcast_pipeline.control_json import load_control_json
from podcast_pipeline.domain.intermediate_formats import TranscriptChunkMeta
from podcast_pipeline.file_inputs import read_regular_utf8, sha256_regular_utf8
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore


@dataclass(frozen=True)
class ChunkerConfig:
    max_tokens: int = 1_200
    overlap_tokens: int = 200
    boundary_lookback_tokens: int = 200
    min_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_tokens", "overlap_tokens", "boundary_lookback_tokens", "min_tokens"):
            value = getattr(self, name)
            if name == "min_tokens" and value is None:
                continue
            if type(value) is not int:
                raise ValueError(f"{name} must be an integer")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        if self.overlap_tokens < 0:
            raise ValueError("overlap_tokens must be >= 0")
        if self.overlap_tokens >= self.max_tokens:
            raise ValueError("overlap_tokens must be < max_tokens")
        if self.boundary_lookback_tokens < 0:
            raise ValueError("boundary_lookback_tokens must be >= 0")
        if self.min_tokens is not None and self.min_tokens < 1:
            raise ValueError("min_tokens must be >= 1 when provided")
        if self.min_tokens is not None and self.min_tokens > self.max_tokens:
            raise ValueError("min_tokens must be <= max_tokens when provided")

    @property
    def effective_min_tokens(self) -> int:
        if self.min_tokens is not None:
            return self.min_tokens
        return max(1, self.max_tokens * 3 // 5)


@dataclass(frozen=True)
class TranscriptChunk:
    chunk_id: int
    start_token: int
    end_token: int
    text: str


class ChunkerResourceError(ValueError):
    """Chunking would exceed its finite work envelope."""


class ChunkRecoveryError(RuntimeError):
    """Retained generations require explicit operator reconciliation."""


class ChunkSourceChangedError(ValueError):
    """The transcript changed while chunks were staged; the current set was kept."""


@dataclass(frozen=True)
class ChunkerLimits:
    source_bytes: int = 8 * 1024 * 1024
    tokens: int = 500_000
    chunks: int = 4096
    output_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} limit must be a positive integer")


DEFAULT_LIMITS = ChunkerLimits()
CHUNK_MANIFEST_MAX_BYTES = 2 * 1024 * 1024
_META_BYTES = 4096
_SOURCE_ADMISSION_ERROR = "Chunker source_bytes limit or coherent regular UTF-8 source required"


def _admit(size: int, ceiling: int, name: str) -> None:
    if size > ceiling:
        raise ChunkerResourceError(f"Chunker {name} limit exceeded; reduce input or overlap and retry")


def _read_source(path: Path, limits: ChunkerLimits) -> str:
    try:
        return read_regular_utf8(path, max_bytes=limits.source_bytes)
    except (OSError, ValueError) as exc:
        raise ChunkerResourceError(_SOURCE_ADMISSION_ERROR) from exc


_TOKEN_RE = re.compile(r"\S+")
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]*$")


@dataclass(frozen=True)
class _Token:
    start: int
    end: int
    byte_start: int


def _tokenize(text: str, limits: ChunkerLimits) -> list[_Token]:
    tokens: list[_Token] = []
    previous = offset = 0
    for match in _TOKEN_RE.finditer(text):
        _admit(len(tokens) + 1, limits.tokens, "tokens")
        offset += len(text[previous : match.start()].encode("utf-8"))
        tokens.append(_Token(match.start(), match.end(), offset))
        previous = match.start()
    return tokens


def _build_boundaries(text: str, tokens: list[_Token]) -> tuple[list[int], list[int], list[int]]:
    paragraph: list[int] = []
    sentence: list[int] = []
    line: list[int] = []

    for idx in range(1, len(tokens)):
        prev = tokens[idx - 1]
        cur = tokens[idx]
        sep = text[prev.end : cur.start]
        if "\n\n" in sep:
            paragraph.append(idx)
            continue
        if "\n" in sep:
            line.append(idx)
        prev_token = text[prev.start : prev.end]
        if _SENTENCE_END_RE.search(prev_token) is not None and sep.strip() == "":
            sentence.append(idx)

    paragraph.sort()
    sentence.sort()
    line.sort()
    return paragraph, sentence, line


def _last_in_range(sorted_values: list[int], lo: int, hi: int) -> int | None:
    if not sorted_values:
        return None

    pos = bisect.bisect_right(sorted_values, hi) - 1
    if pos < 0:
        return None
    value = sorted_values[pos]
    if value < lo:
        return None
    return value


def chunk_transcript_text(
    text: str, *, config: ChunkerConfig, limits: ChunkerLimits = DEFAULT_LIMITS
) -> list[TranscriptChunk]:
    _admit(len(text), limits.source_bytes, "source_bytes")
    source_bytes = len(text.encode("utf-8"))
    _admit(source_bytes, limits.source_bytes, "source_bytes")
    tokens = _tokenize(text, limits)
    if not tokens:
        return []

    paragraph_boundaries, sentence_boundaries, line_boundaries = _build_boundaries(text, tokens)
    total_tokens = len(tokens)

    chunks: list[TranscriptChunk] = []
    start_token = 0
    chunk_id = 1
    output_bytes = 0

    while start_token < total_tokens:
        desired_end = min(start_token + config.max_tokens, total_tokens)

        if desired_end == total_tokens:
            end_token = total_tokens
        else:
            min_chunk_size = max(config.effective_min_tokens, config.overlap_tokens + 1)
            min_end = min(total_tokens, start_token + min_chunk_size)

            search_lo = max(min_end, desired_end - config.boundary_lookback_tokens)
            search_hi = desired_end

            end_token = (
                _last_in_range(paragraph_boundaries, search_lo, search_hi)
                or _last_in_range(sentence_boundaries, search_lo, search_hi)
                or _last_in_range(line_boundaries, search_lo, search_hi)
                or desired_end
            )

        if end_token <= start_token:
            end_token = min(start_token + config.max_tokens, total_tokens)
        if end_token <= start_token:
            raise RuntimeError("chunker failed to make progress")

        start_char = tokens[start_token].start
        if end_token >= total_tokens:
            end_char = len(text)
        else:
            end_char = tokens[end_token].start
        end_byte = tokens[end_token].byte_start if end_token < total_tokens else source_bytes
        output_bytes += end_byte - tokens[start_token].byte_start + (text[end_char - 1] != "\n")
        _admit(chunk_id, limits.chunks, "chunks")
        _admit(output_bytes, limits.output_bytes, "output_bytes")
        chunk_text = text[start_char:end_char]
        if not chunk_text.endswith("\n"):
            chunk_text += "\n"

        chunks.append(
            TranscriptChunk(
                chunk_id=chunk_id,
                start_token=start_token,
                end_token=end_token,
                text=chunk_text,
            ),
        )

        if end_token >= total_tokens:
            break

        next_start = end_token - config.overlap_tokens
        if next_start <= start_token:
            next_start = end_token
        start_token = next_start
        chunk_id += 1

    return chunks


def write_transcript_chunks(
    *,
    layout: EpisodeWorkspaceLayout,
    transcript_path: Path,
    config: ChunkerConfig,
    limits: ChunkerLimits = DEFAULT_LIMITS,
) -> list[TranscriptChunkMeta]:
    with EpisodeWorkspaceStore(layout.root).publication_lock():
        _recover_chunks(layout, limits)
    transcript_text = _read_source(transcript_path, limits)
    source_digest = hashlib.sha256(transcript_text.encode("utf-8")).hexdigest()
    chunks = chunk_transcript_text(transcript_text, config=config, limits=limits)

    destination = layout.transcript_chunks_dir
    destination.parent.mkdir(parents=True, exist_ok=True)
    metas: list[TranscriptChunkMeta] = []
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".chunks-") as temp:
        staged = Path(temp) / "new"
        staged.mkdir()
        files: dict[str, str] = {}
        for chunk in chunks:
            text_path = layout.transcript_chunk_text_path(chunk.chunk_id)
            meta_path = layout.transcript_chunk_meta_json_path(chunk.chunk_id)
            meta = TranscriptChunkMeta(
                chunk_id=chunk.chunk_id,
                text_relpath=text_path.relative_to(layout.root).as_posix(),
            )
            for path, text in (
                (text_path, chunk.text),
                (meta_path, json.dumps(meta.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"),
            ):
                (staged / path.name).write_text(text, encoding="utf-8")
                files[path.name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            metas.append(meta)
        manifest = {
            "version": 1,
            "source_sha256": source_digest,
            "config": asdict(config),
            "chunk_ids": [meta.chunk_id for meta in metas],
            "files": files,
        }
        (staged / "complete.json").write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
        with EpisodeWorkspaceStore(layout.root).publication_lock():
            _recover_chunks(layout, limits)
            try:
                current_digest = sha256_regular_utf8(transcript_path, max_bytes=limits.source_bytes)
            except (OSError, ValueError) as exc:
                raise ChunkerResourceError(_SOURCE_ADMISSION_ERROR) from exc
            if current_digest != source_digest:
                raise ChunkSourceChangedError("Transcript changed while staging chunks; retry with the current source")
            _publish_chunks(staged, destination)
    return metas


def _publish_chunks(staged: Path, destination: Path) -> None:
    """Caller holds the stable workspace lock through swap, rollback and retirement."""
    backup = destination.parent / f".chunks-previous-{uuid4().hex}"
    if destination.exists():
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except BaseException as publication_error:
        if backup.exists():
            try:
                os.replace(backup, destination)
            except BaseException as recovery_error:
                publication_error.add_note(f"Could not restore previous chunks retained at {backup}: {recovery_error}")
        raise
    if backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError:
            # A diagnostics handler cannot undo the committed directory swap.
            with suppress(Exception):
                logging.getLogger(__name__).warning(
                    "Chunk generation committed; old backup retained at %s; verify current chunks before cleanup",
                    backup,
                )


def discover_complete_chunk_ids(
    layout: EpisodeWorkspaceLayout, *, config: ChunkerConfig | None = None, limits: ChunkerLimits = DEFAULT_LIMITS
) -> list[int]:
    """Ignore interrupted/legacy chunks and sets built from another transcript."""
    with EpisodeWorkspaceStore(layout.root).publication_lock():
        _recover_chunks(layout, limits)
        return _validated_chunk_ids(layout.transcript_chunks_dir, layout=layout, config=config, limits=limits) or []


def read_published_chunk_count(layout: EpisodeWorkspaceLayout) -> int | None:
    """Count the published manifest's entries without reading the source or chunk files."""
    try:
        ids, _, _ = _read_chunk_manifest(layout.transcript_chunks_dir, config=None, limits=DEFAULT_LIMITS)
        return len(ids)
    except (OSError, ValueError, KeyError):
        return None


def _recover_chunks(layout: EpisodeWorkspaceLayout, limits: ChunkerLimits) -> None:
    """Restore only one unambiguous validated backup; never promote staging."""
    destination = layout.transcript_chunks_dir
    if destination.exists():
        return
    candidate: Path | None = None
    for backup in destination.parent.glob(".chunks-previous-*"):
        if candidate is not None:
            raise ChunkRecoveryError(
                "Ambiguous chunk backups; retain evidence and reconcile .chunks-previous-* manually"
            )
        candidate = backup
    if candidate is None:
        return
    if (
        candidate.is_symlink()
        or not candidate.is_dir()
        or _validated_chunk_ids(candidate, layout=layout, config=None, limits=limits) is None
    ):
        raise ChunkRecoveryError(f"Unusable chunk backup retained at {candidate}; repair before retrying")
    os.replace(candidate, destination)


def _read_chunk_manifest(
    directory: Path, *, config: ChunkerConfig | None, limits: ChunkerLimits
) -> tuple[list[int], dict[str, str], str]:
    manifest = load_control_json(read_regular_utf8(directory / "complete.json", max_bytes=CHUNK_MANIFEST_MAX_BYTES))
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError("Invalid chunk manifest version")
    if not _matches_config(manifest["config"], config):
        raise ValueError("Invalid or changed chunk configuration")
    source_digest = manifest["source_sha256"]
    if not isinstance(source_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", source_digest):
        raise ValueError("Invalid chunk manifest source digest")
    ids = manifest["chunk_ids"]
    if not isinstance(ids, list) or len(ids) > limits.chunks or any(type(i) is not int for i in ids):
        raise ValueError("Invalid chunk manifest IDs")
    if ids != list(range(1, len(ids) + 1)):
        raise ValueError("Invalid chunk manifest ID sequence")
    files = manifest["files"]
    expected = {f"chunk_{i:04d}{ext}" for i in ids for ext in (".txt", ".json")}
    if not isinstance(files, dict) or files.keys() != expected:
        raise ValueError("Invalid chunk manifest filenames")
    if any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in files.values()):
        raise ValueError("Invalid chunk manifest file digest")
    return ids, files, source_digest


def _validated_chunk_ids(
    directory: Path, *, layout: EpisodeWorkspaceLayout, config: ChunkerConfig | None, limits: ChunkerLimits
) -> list[int] | None:
    try:
        ids, files, source_digest = _read_chunk_manifest(directory, config=config, limits=limits)
        source = _read_source(layout.transcript_dir / "transcript.txt", limits)
        if source_digest != hashlib.sha256(source.encode("utf-8")).hexdigest():
            return None
        for count, _ in enumerate(_TOKEN_RE.finditer(source), start=1):
            _admit(count, limits.tokens, "tokens")
        output_bytes = 0
        for chunk_id in ids:
            meta_name = layout.transcript_chunk_meta_json_path(chunk_id).name
            raw_meta = read_regular_utf8(directory / meta_name, max_bytes=_META_BYTES)
            if hashlib.sha256(raw_meta.encode("utf-8")).hexdigest() != files[meta_name]:
                return None
            meta = TranscriptChunkMeta.model_validate(load_control_json(raw_meta))
            text_path = layout.transcript_chunk_text_path(chunk_id)
            if meta.chunk_id != chunk_id or meta.text_relpath != text_path.relative_to(layout.root).as_posix():
                return None

            data = read_regular_utf8(directory / text_path.name, max_bytes=limits.output_bytes - output_bytes).encode(
                "utf-8"
            )
            output_bytes += len(data)
            if hashlib.sha256(data).hexdigest() != files[text_path.name]:
                return None
        return ids
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _matches_config(recorded: Any, requested: ChunkerConfig | None) -> bool:
    if not isinstance(recorded, dict) or set(recorded) != set(asdict(ChunkerConfig())):
        return False
    validated = ChunkerConfig(**recorded)
    return requested is None or requested == validated
