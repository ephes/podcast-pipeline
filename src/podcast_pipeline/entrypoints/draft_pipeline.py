from __future__ import annotations

import shutil
from pathlib import Path

import typer

from podcast_pipeline.entrypoints.draft_candidates import run_draft_candidates
from podcast_pipeline.entrypoints.summarize_demo import run_summarize_demo
from podcast_pipeline.summarization_stub import StubSummarizerConfig
from podcast_pipeline.transcript_chunker import ChunkerConfig
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def _copy_chapters_into_workspace(*, workspace: Path, chapters: Path) -> None:
    store = EpisodeWorkspaceStore(workspace)
    chapters_path = store.layout.transcript_dir / "chapters.txt"
    chapters_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(chapters, chapters_path)

    episode_yaml = store.read_episode_yaml()
    inputs = episode_yaml.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
    inputs = dict(inputs)
    inputs["chapters"] = str(chapters_path.relative_to(store.layout.root))
    episode_yaml["inputs"] = inputs
    store.write_episode_yaml(episode_yaml)


def run_draft_pipeline(
    *,
    dry_run: bool,
    workspace: Path,
    episode_id: str,
    transcript: Path,
    chapters: Path | None,
    candidates_per_asset: int,
    chunker_config: ChunkerConfig,
    summarizer_config: StubSummarizerConfig,
) -> None:
    if not dry_run:
        typer.echo("Only `podcast draft --dry-run` is implemented right now.", err=True)
        raise typer.Exit(code=2)

    run_summarize_demo(
        dry_run=True,
        workspace=workspace,
        episode_id=episode_id,
        transcript=transcript,
        chunker_config=chunker_config,
        summarizer_config=summarizer_config,
    )

    if chapters is not None:
        _copy_chapters_into_workspace(workspace=workspace, chapters=chapters)

    run_draft_candidates(
        workspace=workspace,
        chapters=None,
        candidates_per_asset=candidates_per_asset,
    )
