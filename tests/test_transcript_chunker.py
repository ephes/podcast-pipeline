from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from podcast_pipeline.transcript_chunker import (
    ChunkerConfig,
    chunk_transcript_text,
    write_transcript_chunks,
)
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


def _tokens(text: str) -> list[str]:
    return re.findall(r"\S+", text)


def test_chunker_returns_no_chunks_for_whitespace_only_input() -> None:
    chunks = chunk_transcript_text(
        " \n\t\n",
        config=ChunkerConfig(max_tokens=10, overlap_tokens=2),
    )
    assert chunks == []


def test_chunker_enforces_overlap_lt_max_tokens() -> None:
    with pytest.raises(ValueError):
        ChunkerConfig(max_tokens=10, overlap_tokens=10)

    with pytest.raises(ValueError):
        ChunkerConfig(max_tokens=10, overlap_tokens=0, min_tokens=11)


def test_chunker_preserves_token_overlap_between_adjacent_chunks() -> None:
    transcript = " ".join(f"w{i:02d}" for i in range(1, 31))
    config = ChunkerConfig(
        max_tokens=10,
        overlap_tokens=3,
        boundary_lookback_tokens=0,
        min_tokens=10,
    )
    chunks = chunk_transcript_text(transcript, config=config)

    assert [c.chunk_id for c in chunks] == [1, 2, 3, 4]

    for prev, nxt in zip(chunks[:-1], chunks[1:], strict=True):
        prev_tokens = _tokens(prev.text)
        nxt_tokens = _tokens(nxt.text)
        assert prev_tokens[-3:] == nxt_tokens[:3]


def test_chunker_prefers_paragraph_boundaries_near_end() -> None:
    transcript = "\n\n".join(
        [
            " ".join(f"a{i}" for i in range(1, 11)),
            " ".join(f"b{i}" for i in range(1, 11)),
        ],
    )
    config = ChunkerConfig(
        max_tokens=10,
        overlap_tokens=0,
        boundary_lookback_tokens=10,
        min_tokens=10,
    )
    chunks = chunk_transcript_text(transcript, config=config)

    assert len(chunks) == 2
    assert _tokens(chunks[0].text) == [f"a{i}" for i in range(1, 11)]
    assert _tokens(chunks[1].text) == [f"b{i}" for i in range(1, 11)]


def test_chunker_prefers_sentence_boundaries_when_available() -> None:
    transcript = "one two three. four five six. seven eight nine ten"
    config = ChunkerConfig(
        max_tokens=6,
        overlap_tokens=0,
        boundary_lookback_tokens=6,
        min_tokens=5,
    )
    chunks = chunk_transcript_text(transcript, config=config)

    assert len(chunks) == 2
    assert _tokens(chunks[0].text) == ["one", "two", "three.", "four", "five", "six."]
    assert _tokens(chunks[1].text) == ["seven", "eight", "nine", "ten"]


def test_write_transcript_chunks_creates_deterministic_chunk_files(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    transcript_path = tmp_path / "inputs" / "transcript.txt"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(
        "a b c d e f g h i j\n\nk l m n o p q r s t\n",
        encoding="utf-8",
    )

    config = ChunkerConfig(
        max_tokens=10,
        overlap_tokens=0,
        boundary_lookback_tokens=10,
        min_tokens=10,
    )
    metas = write_transcript_chunks(
        layout=layout,
        transcript_path=transcript_path,
        config=config,
    )

    assert [meta.chunk_id for meta in metas] == [1, 2]

    chunk1_txt = layout.transcript_chunk_text_path(1)
    chunk1_json = layout.transcript_chunk_meta_json_path(1)
    assert chunk1_txt.exists()
    assert chunk1_json.exists()

    payload = json.loads(chunk1_json.read_text(encoding="utf-8"))
    assert payload["chunk_id"] == 1
    assert payload["text_relpath"] == "transcript/chunks/chunk_0001.txt"

    chunk2_txt = layout.transcript_chunk_text_path(2)
    chunk2_json = layout.transcript_chunk_meta_json_path(2)
    assert chunk2_txt.exists()
    assert chunk2_json.exists()


def test_write_transcript_chunks_preserves_overlap_in_written_files(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    transcript_path = tmp_path / "inputs" / "transcript.txt"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(
        " ".join(f"w{i:02d}" for i in range(1, 31)),
        encoding="utf-8",
    )

    config = ChunkerConfig(
        max_tokens=10,
        overlap_tokens=3,
        boundary_lookback_tokens=0,
        min_tokens=10,
    )
    write_transcript_chunks(
        layout=layout,
        transcript_path=transcript_path,
        config=config,
    )

    chunk1 = layout.transcript_chunk_text_path(1).read_text(encoding="utf-8")
    chunk2 = layout.transcript_chunk_text_path(2).read_text(encoding="utf-8")
    assert _tokens(chunk1)[-3:] == _tokens(chunk2)[:3]


def test_chunker_overlap_tracks_boundary_end_token() -> None:
    transcript = " ".join(f"w{i}" for i in range(1, 7)) + "\n\n" + " ".join(f"w{i}" for i in range(7, 13))
    config = ChunkerConfig(
        max_tokens=8,
        overlap_tokens=2,
        boundary_lookback_tokens=4,
        min_tokens=4,
    )

    chunks = chunk_transcript_text(transcript, config=config)

    assert len(chunks) == 2
    assert chunks[0].end_token == 6
    assert chunks[1].start_token == chunks[0].end_token - config.overlap_tokens
    assert _tokens(chunks[0].text)[-2:] == _tokens(chunks[1].text)[:2]
