from __future__ import annotations

from podcast_pipeline.domain.intermediate_formats import ChunkSummary
from podcast_pipeline.summarization_stub import (
    StubSummarizerConfig,
    reduce_chunk_summaries_to_episode_summary_stub,
)


def test_stub_summary_dedupes_key_points_and_topics_by_case() -> None:
    summaries = [
        ChunkSummary(
            chunk_id=1,
            summary_markdown="## Chunk 1\n\nalpha\n",
            bullets=["Alpha", "Beta"],
            entities=["OpenAI", "Python"],
        ),
        ChunkSummary(
            chunk_id=2,
            summary_markdown="## Chunk 2\n\nbeta\n",
            bullets=["alpha", "Gamma"],
            entities=["openai", "python"],
        ),
    ]
    config = StubSummarizerConfig(max_episode_key_points=10)

    episode = reduce_chunk_summaries_to_episode_summary_stub(
        chunk_summaries=summaries,
        config=config,
    )

    assert episode.key_points == ["Alpha", "Beta", "Gamma"]
    assert episode.topics == ["OpenAI", "Python"]
