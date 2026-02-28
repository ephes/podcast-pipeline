from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

app = typer.Typer(add_completion=False)


@app.command()
def version() -> None:
    """Print version."""
    from podcast_pipeline import __version__

    typer.echo(__version__)


@app.command()
def init(
    *,
    episode_id: Annotated[
        str,
        typer.Option(help="Episode id to create a workspace for."),
    ],
    workspace: Annotated[
        Path | None,
        typer.Option(
            help="Episode workspace directory (default: ./episodes/<episode_id>).",
        ),
    ] = None,
) -> None:
    """Create a new episode workspace with the default layout."""
    from podcast_pipeline.entrypoints.init import run_init

    run_init(workspace=workspace, episode_id=episode_id, project_root=Path.cwd())


@app.command()
def ingest(
    *,
    workspace: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=False,
            help="Episode workspace directory (must exist).",
        ),
    ],
    reaper_media_dir: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=False,
            help="Reaper media directory containing source tracks.",
        ),
    ],
    tracks_glob: Annotated[
        str,
        typer.Option(help="Glob for selecting track files (default: *.flac)."),
    ] = "*.flac",
) -> None:
    """Scan a Reaper media dir and update episode.yaml sources + tracks."""
    from podcast_pipeline.entrypoints.ingest import run_ingest

    run_ingest(
        workspace=workspace,
        reaper_media_dir=reaper_media_dir,
        tracks_glob=tracks_glob,
    )


@app.command()
def transcribe(
    *,
    workspace: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=False,
            help="Episode workspace directory (must exist).",
        ),
    ],
    mode: Annotated[
        str,
        typer.Option(help="Transcript mode to generate (draft or final)."),
    ] = "draft",
    command: Annotated[
        str,
        typer.Option(help="Transcription CLI command (default: podcast-transcript)."),
    ] = "podcast-transcript",
    arg: Annotated[
        list[str] | None,
        typer.Option(
            "--arg",
            help="Extra args for the transcription CLI (supports {mode}, {output_dir}, {workspace}).",
        ),
    ] = None,
) -> None:
    """Run podcast-transcript to generate transcript artifacts."""
    from podcast_pipeline.entrypoints.transcribe import TranscribeConfig, TranscriptionMode, run_transcribe

    try:
        resolved_mode = TranscriptionMode(mode.strip().lower())
    except ValueError as exc:
        raise typer.BadParameter("mode must be 'draft' or 'final'") from exc

    config = TranscribeConfig(command=command, args=tuple(arg) if arg else None)
    run_transcribe(
        workspace=workspace,
        mode=resolved_mode,
        config=config,
    )


@app.command()
def draft(
    *,
    workspace: Annotated[
        Path,
        typer.Option(
            help="Episode workspace directory (created if needed, reused if exists).",
        ),
    ],
    transcript: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="Transcript .txt file to ingest (optional if workspace already has chunks).",
        ),
    ] = None,
    chapters: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="Optional chapters .txt file.",
        ),
    ] = None,
    candidates_per_asset: Annotated[
        int,
        typer.Option("--candidates", "-n", min=1, help="Candidates per asset."),
    ] = 3,
    dry_run: Annotated[
        bool,
        typer.Option(help="Run draft without any external LLM calls (stub backend)."),
    ] = False,
    episode_id: Annotated[
        str,
        typer.Option(help="Episode id to write into the workspace."),
    ] = "demo_ep_001",
    timeout: Annotated[
        float | None,
        typer.Option(min=0, help="Timeout in seconds for each LLM CLI call."),
    ] = None,
    host: Annotated[
        list[str] | None,
        typer.Option(
            "--host",
            help="Host/speaker name (repeatable, e.g. --host Jochen --host Dominik).",
        ),
    ] = None,
) -> None:
    """Create draft candidates by running the text pipeline."""
    from podcast_pipeline.entrypoints.draft_pipeline import run_draft_pipeline
    from podcast_pipeline.summarization_stub import StubSummarizerConfig
    from podcast_pipeline.transcript_chunker import ChunkerConfig

    run_draft_pipeline(
        dry_run=dry_run,
        workspace=workspace,
        episode_id=episode_id,
        transcript=transcript,
        chapters=chapters,
        candidates_per_asset=candidates_per_asset,
        chunker_config=ChunkerConfig(),
        summarizer_config=StubSummarizerConfig(),
        timeout_seconds=timeout,
        hosts=host if host else None,
    )


@app.command()
def review(
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
    """Run the Creator/Reviewer loop for one asset."""
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
def pick(
    *,
    workspace: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=False,
            help="Episode workspace directory (must exist).",
        ),
    ],
    asset_id: Annotated[
        str | None,
        typer.Option(help="Asset id to pick (default: all assets with candidates)."),
    ] = None,
    candidate_id: Annotated[
        UUID | None,
        typer.Option(help="Candidate id to select (requires --asset-id)."),
    ] = None,
    web: Annotated[
        bool,
        typer.Option(help="Open a local web UI for full-text candidate comparison."),
    ] = False,
) -> None:
    """Select a candidate per asset and write copy/selected outputs."""
    if web:
        if candidate_id is not None:
            raise typer.BadParameter("--candidate-id is not supported with --web")
        from podcast_pipeline.entrypoints.pick_web import run_pick_web

        run_pick_web(workspace=workspace, asset_id=asset_id)
    else:
        from podcast_pipeline.entrypoints.pick import run_pick

        run_pick(workspace=workspace, asset_id=asset_id, candidate_id=candidate_id)


@app.command()
def produce(
    *,
    workspace: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=False,
            help="Episode workspace directory (default: current directory).",
        ),
    ] = Path("."),
    dry_run: Annotated[
        bool,
        typer.Option(help="Print the Auphonic payload JSON without calling the API."),
    ] = False,
) -> None:
    """Build the Auphonic payload for an episode workspace."""
    from podcast_pipeline.entrypoints.produce import run_produce

    run_produce(workspace=workspace, dry_run=dry_run)


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
def rss_examples(
    *,
    feed_url: Annotated[
        str,
        typer.Option(help="RSS 2.0 feed URL to fetch."),
    ],
    output: Annotated[
        Path,
        typer.Option(help="Output JSONL path (few-shot dataset)."),
    ] = Path("datasets/rss_examples.jsonl"),
    limit: Annotated[
        int,
        typer.Option(min=1, help="Number of recent episodes to fetch."),
    ] = 25,
    timeout_seconds: Annotated[
        float,
        typer.Option(help="HTTP timeout in seconds."),
    ] = 20.0,
) -> None:
    """Fetch recent RSS episodes and write a JSONL few-shot dataset."""
    from podcast_pipeline.entrypoints.rss_examples import run_rss_examples

    run_rss_examples(
        feed_url=feed_url,
        output=output,
        limit=limit,
        timeout_seconds=timeout_seconds,
    )


@app.command()
def cms_examples(
    *,
    api_url: Annotated[
        str,
        typer.Option(help="Wagtail/CMS API URL (episodes endpoint)."),
    ],
    output: Annotated[
        Path,
        typer.Option(help="Output JSONL path (few-shot dataset)."),
    ] = Path("datasets/cms_examples.jsonl"),
    limit: Annotated[
        int,
        typer.Option(min=1, help="Number of recent episodes to fetch."),
    ] = 25,
    timeout_seconds: Annotated[
        float,
        typer.Option(help="HTTP timeout in seconds."),
    ] = 20.0,
    title_field: Annotated[
        str,
        typer.Option(help="Field name for the episode title."),
    ] = "title",
    summary_field: Annotated[
        str,
        typer.Option(help="Field name for the episode summary."),
    ] = "summary",
    description_field: Annotated[
        str,
        typer.Option(help="Field name for the episode description RichText."),
    ] = "description",
    shownotes_field: Annotated[
        str,
        typer.Option(help="Field name for the episode shownotes RichText."),
    ] = "shownotes",
    tags_field: Annotated[
        str,
        typer.Option(help="Field name for the CMS tags list."),
    ] = "tags",
    link_field: Annotated[
        str,
        typer.Option(help="Field name for the public URL."),
    ] = "url",
    slug_field: Annotated[
        str,
        typer.Option(help="Field name for the page slug."),
    ] = "slug",
    published_field: Annotated[
        str,
        typer.Option(help="Field name for the publish timestamp."),
    ] = "first_published_at",
    page_id_field: Annotated[
        str,
        typer.Option(help="Field name for the CMS page id."),
    ] = "id",
) -> None:
    """Fetch recent CMS episodes and write a JSONL few-shot dataset."""
    from podcast_pipeline.entrypoints.cms_examples import run_cms_examples

    run_cms_examples(
        api_url=api_url,
        output=output,
        limit=limit,
        timeout_seconds=timeout_seconds,
        title_field=title_field,
        summary_field=summary_field,
        description_field=description_field,
        shownotes_field=shownotes_field,
        tags_field=tags_field,
        link_field=link_field,
        slug_field=slug_field,
        published_field=published_field,
        page_id_field=page_id_field,
    )


@app.command()
def dashboard(
    *,
    workspace: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=False,
            help="Episode workspace directory (must exist).",
        ),
    ],
) -> None:
    """Launch the web dashboard for the pipeline."""
    from podcast_pipeline.entrypoints.dashboard_web import run_dashboard

    run_dashboard(workspace=workspace)


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
