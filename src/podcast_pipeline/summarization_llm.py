from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import suppress

import typer

from podcast_pipeline.domain.intermediate_formats import ChunkSummary, EpisodeSummary
from podcast_pipeline.domain.models import ProvenanceRef
from podcast_pipeline.drafter_runner import DrafterRunner
from podcast_pipeline.file_inputs import read_regular_utf8
from podcast_pipeline.prompting import (
    PromptRenderer,
    render_chunk_summary_prompt,
    render_episode_summary_prompt,
)
from podcast_pipeline.summarization_stub import write_episode_summary_artifacts
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, atomic_write_text

MAX_CHUNK_SOURCE_BYTES = 256 * 1024
MAX_SUMMARIZER_PROMPT_BYTES = 256 * 1024


class SummarizationResourceError(RuntimeError):
    pass


def _text_bytes(text: str) -> int:
    if len(text) > MAX_SUMMARIZER_PROMPT_BYTES:
        raise SummarizationResourceError("Summarizer prompt byte limit exceeded; reduce context and retry.")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError as exc:
        raise SummarizationResourceError("Summarizer context must be valid UTF-8.") from exc
    if size > MAX_SUMMARIZER_PROMPT_BYTES:
        raise SummarizationResourceError("Summarizer prompt byte limit exceeded; reduce context and retry.")
    return size


def _admit_hosts(hosts: Sequence[str] | None) -> None:
    total = 0
    for host in hosts or ():
        total += _text_bytes(host) + 2
        if total > MAX_SUMMARIZER_PROMPT_BYTES:
            raise SummarizationResourceError("Summarizer hosts exceed prompt byte limit.")


def _bounded_summaries_json(chunk_summaries: list[ChunkSummary]) -> str:
    # Serialize one model at a time, charging fragments before retaining/combining them.
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    parts = ["["]
    size = 2
    for index, summary in enumerate(chunk_summaries):
        if index:
            parts.append(",")
            size += 1
        for part in encoder.iterencode(summary.model_dump(mode="json")):
            size += _text_bytes(part)
            if size > MAX_SUMMARIZER_PROMPT_BYTES:
                raise SummarizationResourceError("Summary reduction source exceeds prompt byte limit.")
            parts.append(part)
    parts.append("]")
    return "".join(parts)


def _echo_progress(message: str) -> None:
    # stderr may be broken or closed while model work can still complete.
    with suppress(OSError, ValueError):
        typer.echo(message, err=True)


def summarize_transcript_chunks_llm(
    *,
    layout: EpisodeWorkspaceLayout,
    chunk_ids: list[int],
    runner: DrafterRunner,
    renderer: PromptRenderer,
    hosts: Sequence[str] | None = None,
) -> list[ChunkSummary]:
    """Summarize unique transcript chunks via LLM calls in the given order."""
    seen_ids: set[int] = set()
    for chunk_id in chunk_ids:
        if chunk_id in seen_ids:
            raise ValueError(f"Duplicate summary chunk ID: {chunk_id}")
        seen_ids.add(chunk_id)

    summaries: list[ChunkSummary] = []
    _admit_hosts(hosts)

    for chunk_id in chunk_ids:
        chunk_path = layout.transcript_chunk_text_path(chunk_id)
        try:
            chunk_text = read_regular_utf8(chunk_path, max_bytes=MAX_CHUNK_SOURCE_BYTES)
        except (OSError, ValueError) as exc:
            raise SummarizationResourceError(
                "Unusable chunk source: regular UTF-8 byte limit or read generation."
            ) from exc

        prompt = render_chunk_summary_prompt(
            renderer=renderer,
            chunk_id=chunk_id,
            chunk_text=chunk_text,
            hosts=hosts,
        )
        _text_bytes(prompt.text)
        _echo_progress(f"  Summarizing chunk {chunk_id}...")
        payload = runner.run(prompt.text)

        payload.setdefault("chunk_id", chunk_id)
        payload.setdefault("version", 1)
        payload.setdefault(
            "provenance",
            [ProvenanceRef(kind="llm_summarizer", ref="chunk_v1").model_dump(mode="json")],
        )

        summary = ChunkSummary.model_validate(payload)
        if summary.chunk_id != chunk_id:
            raise ValueError(f"Chunk summary identity does not match requested chunk {chunk_id}")

        out_path = layout.chunk_summary_json_path(chunk_id)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        dumped = json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        atomic_write_text(out_path, dumped)

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
    _admit_hosts(hosts)
    summaries_json = _bounded_summaries_json(chunk_summaries)

    prompt = render_episode_summary_prompt(
        renderer=renderer,
        chunk_summaries_json=summaries_json,
        hosts=hosts,
    )
    _text_bytes(prompt.text)
    _echo_progress("  Generating episode summary...")
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
