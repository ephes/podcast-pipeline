from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import typer

from podcast_pipeline.domain.models import EpisodeWorkspace
from podcast_pipeline.summarization_stub import (
    StubSummarizerConfig,
    reduce_chunk_summaries_to_episode_summary_stub,
    summarize_transcript_chunks_stub,
    write_episode_summary_artifacts,
)
from podcast_pipeline.transcript_chunker import ChunkerConfig, write_transcript_chunks
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore

_DEMO_CREATED_AT = datetime(2000, 1, 1, 0, 0, 0, tzinfo=UTC)


def run_summarize_demo(
    *,
    dry_run: bool,
    workspace: Path,
    episode_id: str,
    transcript: Path,
    chunker_config: ChunkerConfig,
    summarizer_config: StubSummarizerConfig,
) -> None:
    if not dry_run:
        typer.echo("Only `podcast summarize --dry-run` is implemented right now.", err=True)
        raise typer.Exit(code=2)

    if workspace.exists():
        raise typer.BadParameter(f"workspace already exists: {workspace}")

    workspace.mkdir(parents=True, exist_ok=False)
    store = EpisodeWorkspaceStore(workspace)

    transcript_dir = store.layout.transcript_dir
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcript_dir / "transcript.txt"
    shutil.copyfile(transcript, transcript_path)

    store.write_episode_yaml(
        {
            "episode_id": episode_id,
            "inputs": {"transcript": str(transcript_path.relative_to(store.layout.root))},
        },
    )
    store.write_state(
        EpisodeWorkspace(
            episode_id=episode_id,
            root_dir=".",
            created_at=_DEMO_CREATED_AT,
        ),
    )

    metas = write_transcript_chunks(
        layout=store.layout,
        transcript_path=transcript_path,
        config=chunker_config,
    )
    chunk_ids = [meta.chunk_id for meta in metas]

    chunk_summaries = summarize_transcript_chunks_stub(
        layout=store.layout,
        chunk_ids=chunk_ids,
        config=summarizer_config,
    )
    episode_summary = reduce_chunk_summaries_to_episode_summary_stub(
        chunk_summaries=chunk_summaries,
        config=summarizer_config,
    )
    write_episode_summary_artifacts(layout=store.layout, episode_summary=episode_summary)

    typer.echo(f"Workspace: {workspace}")
    typer.echo(f"Episode summary: {store.layout.episode_summary_markdown_path()}")
