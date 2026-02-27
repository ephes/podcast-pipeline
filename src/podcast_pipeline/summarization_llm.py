from __future__ import annotations

import json
from collections.abc import Sequence

import typer

from podcast_pipeline.domain.intermediate_formats import ChunkSummary, EpisodeSummary
from podcast_pipeline.domain.models import ProvenanceRef
from podcast_pipeline.drafter_runner import DrafterRunner
from podcast_pipeline.prompting import (
    PromptRenderer,
    render_chunk_summary_prompt,
    render_episode_summary_prompt,
)
from podcast_pipeline.summarization_stub import write_episode_summary_artifacts
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


def summarize_transcript_chunks_llm(
    *,
    layout: EpisodeWorkspaceLayout,
    chunk_ids: list[int],
    runner: DrafterRunner,
    renderer: PromptRenderer,
    hosts: Sequence[str] | None = None,
) -> list[ChunkSummary]:
    """Summarize each transcript chunk via an LLM call."""
    summaries: list[ChunkSummary] = []

    for chunk_id in chunk_ids:
        chunk_path = layout.transcript_chunk_text_path(chunk_id)
        chunk_text = chunk_path.read_text(encoding="utf-8")

        prompt = render_chunk_summary_prompt(
            renderer=renderer,
            chunk_id=chunk_id,
            chunk_text=chunk_text,
            hosts=hosts,
        )
        typer.echo(f"  Summarizing chunk {chunk_id}...", err=True)
        payload = runner.run(prompt.text)

        payload.setdefault("chunk_id", chunk_id)
        payload.setdefault("version", 1)
        payload.setdefault(
            "provenance",
            [ProvenanceRef(kind="llm_summarizer", ref="chunk_v1").model_dump(mode="json")],
        )

        summary = ChunkSummary.model_validate(payload)

        out_path = layout.chunk_summary_json_path(chunk_id)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        dumped = json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        out_path.write_text(dumped, encoding="utf-8")

        summaries.append(summary)

    return summaries


def reduce_chunk_summaries_to_episode_summary_llm(
    *,
    chunk_summaries: list[ChunkSummary],
    runner: DrafterRunner,
    renderer: PromptRenderer,
    hosts: Sequence[str] | None = None,
) -> EpisodeSummary:
    """Reduce all chunk summaries into a single episode summary via LLM."""
    summaries_data = [s.model_dump(mode="json") for s in chunk_summaries]
    summaries_json = json.dumps(summaries_data, indent=2, ensure_ascii=False)

    prompt = render_episode_summary_prompt(
        renderer=renderer,
        chunk_summaries_json=summaries_json,
        hosts=hosts,
    )
    typer.echo("  Generating episode summary...", err=True)
    payload = runner.run(prompt.text)

    payload.setdefault("version", 1)
    payload.setdefault(
        "provenance",
        [ProvenanceRef(kind="llm_summarizer", ref="episode_v1").model_dump(mode="json")],
    )

    return EpisodeSummary.model_validate(payload)


def run_llm_summarization(
    *,
    layout: EpisodeWorkspaceLayout,
    chunk_ids: list[int],
    runner: DrafterRunner,
    renderer: PromptRenderer,
    hosts: Sequence[str] | None = None,
) -> EpisodeSummary:
    """Run the full LLM summarization pipeline: chunk summaries → episode summary.

    Writes all artifacts to the workspace and returns the episode summary.
    """
    chunk_summaries = summarize_transcript_chunks_llm(
        layout=layout,
        chunk_ids=chunk_ids,
        runner=runner,
        renderer=renderer,
        hosts=hosts,
    )

    episode_summary = reduce_chunk_summaries_to_episode_summary_llm(
        chunk_summaries=chunk_summaries,
        runner=runner,
        renderer=renderer,
        hosts=hosts,
    )

    write_episode_summary_artifacts(layout=layout, episode_summary=episode_summary)

    return episode_summary
