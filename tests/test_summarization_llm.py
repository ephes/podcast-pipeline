from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest

from podcast_pipeline.domain.intermediate_formats import ChunkSummary
from podcast_pipeline.prompting import PromptRenderer, default_prompt_registry
from podcast_pipeline.summarization_llm import (
    reduce_chunk_summaries_to_episode_summary_llm,
    run_llm_summarization,
    summarize_transcript_chunks_llm,
)
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


class FakeDrafterRunner:
    """Test double that returns scripted JSON responses."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self._pos = 0
        self.prompts: list[str] = []

    def run(self, prompt_text: str) -> dict[str, Any]:
        self.prompts.append(prompt_text)
        if self._pos >= len(self._responses):
            raise IndexError(f"FakeDrafterRunner exhausted after {self._pos} calls")
        resp = self._responses[self._pos]
        self._pos += 1
        return dict(resp)


def test_summarize_transcript_chunks_llm(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    chunks_dir = layout.transcript_chunks_dir
    chunks_dir.mkdir(parents=True)

    # Write two chunk text files
    layout.transcript_chunk_text_path(1).write_text("Hallo, willkommen zum Podcast.\n")
    layout.transcript_chunk_text_path(2).write_text("Heute besprechen wir Thema X.\n")

    # Scripted responses for each chunk
    responses: list[dict[str, Any]] = [
        {
            "chunk_id": 1,
            "summary_markdown": "## Chunk 1\n\nBegrüßung\n",
            "bullets": ["Willkommen"],
            "entities": ["Podcast"],
        },
        {
            "chunk_id": 2,
            "summary_markdown": "## Chunk 2\n\nThema X\n",
            "bullets": ["Thema X besprochen"],
            "entities": ["Thema X"],
        },
    ]

    runner = FakeDrafterRunner(list(reversed(responses)))
    renderer = PromptRenderer(default_prompt_registry())

    summaries = summarize_transcript_chunks_llm(
        layout=layout,
        chunk_ids=[2, 1],
        runner=runner,
        renderer=renderer,
    )

    assert [summary.chunk_id for summary in summaries] == [2, 1]
    assert summaries[1].bullets == ["Willkommen"]
    assert summaries[0].entities == ["Thema X"]

    # Check artifacts were written
    assert layout.chunk_summary_json_path(1).exists()
    assert layout.chunk_summary_json_path(2).exists()

    # Verify runner received 2 prompts
    assert len(runner.prompts) == 2


def test_reduce_chunk_summaries_to_episode_summary_llm() -> None:
    chunk_summaries = [
        ChunkSummary(
            chunk_id=1,
            summary_markdown="## Chunk 1\n\nBegrüßung\n",
            bullets=["Willkommen"],
            entities=["Podcast"],
        ),
        ChunkSummary(
            chunk_id=2,
            summary_markdown="## Chunk 2\n\nThema X\n",
            bullets=["Thema X"],
            entities=["Thema X"],
        ),
    ]

    response: dict[str, Any] = {
        "summary_markdown": "# Episode\n\nBegrüßung und Thema X\n",
        "key_points": ["Willkommen", "Thema X besprochen"],
        "topics": ["Podcast", "Thema X"],
    }

    runner = FakeDrafterRunner([response])
    renderer = PromptRenderer(default_prompt_registry())

    episode = reduce_chunk_summaries_to_episode_summary_llm(
        chunk_summaries=chunk_summaries,
        runner=runner,
        renderer=renderer,
    )

    assert episode.key_points == ["Willkommen", "Thema X besprochen"]
    assert episode.topics == ["Podcast", "Thema X"]
    assert "Begrüßung" in episode.summary_markdown

    # Verify the prompt included chunk summaries
    assert len(runner.prompts) == 1
    assert "chunk_id" in runner.prompts[0]


def test_duplicate_chunk_ids_are_rejected_before_reading_sources(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    runner = FakeDrafterRunner([])

    with pytest.raises(ValueError, match="Duplicate summary chunk ID: 2"):
        run_llm_summarization(
            layout=layout,
            chunk_ids=[2, 1, 2],
            runner=runner,
            renderer=PromptRenderer(default_prompt_registry()),
        )

    assert not runner.prompts
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("stream_error", [BrokenPipeError, ValueError])
def test_summary_progress_write_failure_preserves_all_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream_error: type[Exception]
) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    layout.transcript_chunks_dir.mkdir(parents=True)
    for chunk_id in (1, 2):
        layout.transcript_chunk_text_path(chunk_id).write_text(f"Transcript {chunk_id}", encoding="utf-8")

    failed_writes: list[str] = []

    class FailingProgressStream(io.StringIO):
        def write(self, text: str) -> int:
            if layout.chunk_summary_json_path(1).exists():
                failed_writes.append(text.strip())
                raise stream_error("Progress stream unavailable")
            return super().write(text)

    progress = FailingProgressStream()
    monkeypatch.setattr(sys, "stderr", progress)
    runner = FakeDrafterRunner(
        [
            {"summary_markdown": "First chunk"},
            {"summary_markdown": "Second chunk"},
            {"summary_markdown": "Episode summary"},
        ]
    )

    summary = run_llm_summarization(
        layout=layout,
        chunk_ids=[1, 2],
        runner=runner,
        renderer=PromptRenderer(default_prompt_registry()),
    )

    assert "Summarizing chunk 1" in progress.getvalue()
    assert failed_writes == ["Summarizing chunk 2...", "Generating episode summary..."]
    assert len(runner.prompts) == 3
    assert "First chunk" in runner.prompts[2]
    assert "Second chunk" in runner.prompts[2]
    assert summary.summary_markdown == "Episode summary"
    assert layout.chunk_summary_json_path(1).is_file()
    assert layout.chunk_summary_json_path(2).is_file()
    assert layout.episode_summary_json_path().is_file()
    assert layout.episode_summary_markdown_path().is_file()
    assert layout.episode_summary_html_path().is_file()
