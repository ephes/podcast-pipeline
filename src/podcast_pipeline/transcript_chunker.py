from __future__ import annotations

import bisect
import json
import re
from dataclasses import dataclass
from pathlib import Path

from podcast_pipeline.domain.intermediate_formats import TranscriptChunkMeta
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


@dataclass(frozen=True)
class ChunkerConfig:
    max_tokens: int = 1_200
    overlap_tokens: int = 200
    boundary_lookback_tokens: int = 200
    min_tokens: int | None = None

    def __post_init__(self) -> None:
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
        return max(1, int(self.max_tokens * 0.6))


@dataclass(frozen=True)
class TranscriptChunk:
    chunk_id: int
    start_token: int
    end_token: int
    text: str


_TOKEN_RE = re.compile(r"\S+")
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\\]]*$")


@dataclass(frozen=True)
class _Token:
    start: int
    end: int


def _tokenize(text: str) -> list[_Token]:
    return [_Token(m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]


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


def chunk_transcript_text(text: str, *, config: ChunkerConfig) -> list[TranscriptChunk]:
    tokens = _tokenize(text)
    if not tokens:
        return []

    paragraph_boundaries, sentence_boundaries, line_boundaries = _build_boundaries(text, tokens)
    total_tokens = len(tokens)

    chunks: list[TranscriptChunk] = []
    start_token = 0
    chunk_id = 1

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
) -> list[TranscriptChunkMeta]:
    transcript_text = transcript_path.read_text(encoding="utf-8")
    chunks = chunk_transcript_text(transcript_text, config=config)

    metas: list[TranscriptChunkMeta] = []
    for chunk in chunks:
        text_path = layout.transcript_chunk_text_path(chunk.chunk_id)
        meta_path = layout.transcript_chunk_meta_json_path(chunk.chunk_id)

        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(chunk.text, encoding="utf-8")

        text_relpath = text_path.relative_to(layout.root).as_posix()
        meta = TranscriptChunkMeta(chunk_id=chunk.chunk_id, text_relpath=text_relpath)

        dumped = json.dumps(meta.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        meta_path.write_text(dumped, encoding="utf-8")
        metas.append(meta)

    return metas
