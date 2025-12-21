from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(add_completion=False)


@app.command()
def version() -> None:
    """Print version."""
    from podcast_pipeline import __version__

    typer.echo(__version__)


@app.command()
def draft(
    *,
    fake_runner: Annotated[
        bool,
        typer.Option(
            help="Use built-in fake creator/reviewer runners (no Codex/Claude required).",
        ),
    ] = False,
    workspace: Annotated[
        Path | None,
        typer.Option(
            help="Episode workspace directory (default: create a new ./demo_workspace* directory).",
        ),
    ] = None,
    episode_id: Annotated[
        str,
        typer.Option(help="Episode id to write into the workspace."),
    ] = "demo_ep_001",
    asset_id: Annotated[
        str,
        typer.Option(help="Asset id to draft (default: description)."),
    ] = "description",
    max_iterations: Annotated[int, typer.Option(min=1, help="Maximum loop iterations.")] = 3,
) -> None:
    """Create a draft asset by running the Creator/Reviewer loop."""
    from podcast_pipeline.entrypoints.draft_demo import run_draft_demo

    run_draft_demo(
        fake_runner=fake_runner,
        workspace=workspace,
        episode_id=episode_id,
        asset_id=asset_id,
        max_iterations=max_iterations,
    )


@app.command()
def draft_candidates(
    *,
    workspace: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=False,
            help="Episode workspace directory (must exist).",
        ),
    ],
    chapters: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="Optional chapters .txt file (default: read from workspace).",
        ),
    ] = None,
    candidates_per_asset: Annotated[
        int,
        typer.Option("--candidates", "-n", min=1, help="Candidates per asset."),
    ] = 3,
) -> None:
    """Generate N candidate assets from episode summary + chapters."""
    from podcast_pipeline.entrypoints.draft_candidates import run_draft_candidates

    run_draft_candidates(
        workspace=workspace,
        chapters=chapters,
        candidates_per_asset=candidates_per_asset,
    )


@app.command()
def summarize(
    *,
    workspace: Annotated[
        Path,
        typer.Option(help="Episode workspace directory (must not exist)."),
    ],
    transcript: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="Transcript .txt file to ingest.",
        ),
    ],
    dry_run: Annotated[
        bool,
        typer.Option(help="Run summarization without any external LLM calls (stub backend)."),
    ] = False,
    episode_id: Annotated[
        str,
        typer.Option(help="Episode id to write into the workspace."),
    ] = "demo_ep_001",
) -> None:
    """Chunk transcript and write stub summaries into the workspace."""
    from podcast_pipeline.entrypoints.summarize_demo import run_summarize_demo
    from podcast_pipeline.summarization_stub import StubSummarizerConfig
    from podcast_pipeline.transcript_chunker import ChunkerConfig

    run_summarize_demo(
        dry_run=dry_run,
        workspace=workspace,
        episode_id=episode_id,
        transcript=transcript,
        chunker_config=ChunkerConfig(),
        summarizer_config=StubSummarizerConfig(),
    )


@app.command()
def status(
    *,
    workspace: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=False,
            help="Episode workspace directory (default: current directory).",
        ),
    ] = Path("."),
) -> None:
    """Show review loop progress for each asset."""
    from podcast_pipeline.entrypoints.status import run_status

    run_status(workspace=workspace)


def main() -> None:
    app()
