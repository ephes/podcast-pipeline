from __future__ import annotations

from pathlib import Path
from typing import Any

from podcast_pipeline.domain.intermediate_formats import ChunkSummary
from podcast_pipeline.prompting import PromptRenderer, default_prompt_registry
from podcast_pipeline.summarization_llm import (
    reduce_chunk_summaries_to_episode_summary_llm,
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

    runner = FakeDrafterRunner(responses)
    renderer = PromptRenderer(default_prompt_registry())

    summaries = summarize_transcript_chunks_llm(
        layout=layout,
        chunk_ids=[1, 2],
        runner=runner,
        renderer=renderer,
    )

    assert len(summaries) == 2
    assert summaries[0].chunk_id == 1
    assert summaries[1].chunk_id == 2
    assert summaries[0].bullets == ["Willkommen"]
    assert summaries[1].entities == ["Thema X"]

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
