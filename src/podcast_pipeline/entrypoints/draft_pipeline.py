from __future__ import annotations

import json
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


def _discover_chunk_ids(store: EpisodeWorkspaceStore) -> list[int]:
    """Find existing chunk files in the workspace and return sorted chunk ids."""
    chunks_dir = store.layout.transcript_chunks_dir
    if not chunks_dir.exists():
        return []
    ids: list[int] = []
    for path in sorted(chunks_dir.glob("chunk_*.txt")):
        stem = path.stem
        try:
            chunk_id = int(stem.replace("chunk_", ""))
            ids.append(chunk_id)
        except ValueError:
            continue
    return sorted(ids)


def _load_chapters_lines(store: EpisodeWorkspaceStore) -> list[str]:
    """Load chapter lines from workspace (episode.yaml inputs or fallback)."""
    episode_yaml = store.read_episode_yaml()
    raw_inputs = episode_yaml.get("inputs")
    if isinstance(raw_inputs, dict):
        raw_chapters = raw_inputs.get("chapters")
        if isinstance(raw_chapters, str):
            candidate = store.layout.root / raw_chapters
            if candidate.exists() and candidate.is_file():
                return _first_non_empty_lines(candidate.read_text(encoding="utf-8"), limit=200)

    fallback = store.layout.transcript_dir / "chapters.txt"
    if fallback.exists() and fallback.is_file():
        return _first_non_empty_lines(fallback.read_text(encoding="utf-8"), limit=200)

    return []


def _first_non_empty_lines(text: str, *, limit: int) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        lines.append(stripped)
        if len(lines) >= limit:
            break
    return lines


def _open_or_create_workspace(
    *,
    workspace: Path,
    episode_id: str,
    transcript: Path | None,
) -> EpisodeWorkspaceStore:
    """Open an existing workspace or create a new one."""
    if workspace.exists():
        store = EpisodeWorkspaceStore(workspace)
        typer.echo(f"Using existing workspace: {workspace}", err=True)
    else:
        if transcript is None:
            typer.echo("--transcript is required when creating a new workspace", err=True)
            raise typer.Exit(code=2)
        workspace.mkdir(parents=True, exist_ok=False)
        store = EpisodeWorkspaceStore(workspace)
        store.write_episode_yaml({"episode_id": episode_id})
        typer.echo(f"Created workspace: {workspace}", err=True)
    return store


def _clear_stale_artifacts(store: EpisodeWorkspaceStore) -> None:
    """Remove existing chunks and summaries so they are rebuilt from a new transcript."""
    chunks_dir = store.layout.transcript_chunks_dir
    if chunks_dir.exists():
        shutil.rmtree(chunks_dir)
        typer.echo("Cleared stale chunks", err=True)

    summaries_dir = store.layout.summaries_dir
    if summaries_dir.exists():
        shutil.rmtree(summaries_dir)
        typer.echo("Cleared stale summaries", err=True)


def _ingest_transcript(*, store: EpisodeWorkspaceStore, transcript: Path) -> None:
    """Copy a transcript file into the workspace and invalidate stale artifacts."""
    _clear_stale_artifacts(store)

    transcript_dir = store.layout.transcript_dir
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript_dest = transcript_dir / "transcript.txt"
    shutil.copyfile(transcript, transcript_dest)

    episode_yaml = store.read_episode_yaml()
    inputs = episode_yaml.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
    inputs = dict(inputs)
    inputs["transcript"] = str(transcript_dest.relative_to(store.layout.root))
    episode_yaml["inputs"] = inputs
    store.write_episode_yaml(episode_yaml)


def _run_llm_pipeline(
    *,
    store: EpisodeWorkspaceStore,
    candidates_per_asset: int,
    chunker_config: ChunkerConfig,
    timeout_seconds: float | None,
) -> None:
    """Run the real LLM-backed draft pipeline."""
    from podcast_pipeline.agent_cli_config import load_agent_cli_bundle
    from podcast_pipeline.asset_candidates_llm import generate_draft_candidates_llm
    from podcast_pipeline.drafter_runner import DrafterCliRunner
    from podcast_pipeline.prompting import PromptRenderer, default_prompt_registry
    from podcast_pipeline.summarization_llm import run_llm_summarization
    from podcast_pipeline.transcript_chunker import write_transcript_chunks

    workspace = store.layout.root

    bundle = load_agent_cli_bundle(workspace=workspace)
    renderer = PromptRenderer(default_prompt_registry())
    runner = DrafterCliRunner(
        config=bundle.drafter,
        timeout_seconds=timeout_seconds,
        cwd=str(workspace),
    )

    # Discover or create chunks
    chunk_ids = _discover_chunk_ids(store)
    if not chunk_ids:
        transcript_path = store.layout.transcript_dir / "transcript.txt"
        if not transcript_path.exists():
            typer.echo(f"No transcript found at {transcript_path}", err=True)
            raise typer.Exit(code=2)
        typer.echo("Chunking transcript...", err=True)
        metas = write_transcript_chunks(
            layout=store.layout,
            transcript_path=transcript_path,
            config=chunker_config,
        )
        chunk_ids = [meta.chunk_id for meta in metas]

    typer.echo(f"Found {len(chunk_ids)} chunks", err=True)

    # Check if episode summary already exists
    summary_path = store.layout.episode_summary_json_path()
    if summary_path.exists():
        typer.echo("Episode summary already exists, skipping summarization", err=True)
        from podcast_pipeline.domain.intermediate_formats import EpisodeSummary

        episode_summary = EpisodeSummary.model_validate(
            json.loads(summary_path.read_text(encoding="utf-8")),
        )
    else:
        typer.echo("Running LLM summarization...", err=True)
        episode_summary = run_llm_summarization(
            layout=store.layout,
            chunk_ids=chunk_ids,
            runner=runner,
            renderer=renderer,
        )

    # Generate candidates
    typer.echo("Generating asset candidates...", err=True)
    chapters_lines = _load_chapters_lines(store)
    assets = generate_draft_candidates_llm(
        episode_summary=episode_summary,
        chapters=chapters_lines,
        candidates_per_asset=candidates_per_asset,
        runner=runner,
        renderer=renderer,
    )

    written = 0
    for _asset_id, candidates in sorted(assets.items()):
        for candidate in candidates:
            store.write_candidate(candidate)
            written += 1

    typer.echo(f"Workspace: {workspace}")
    typer.echo(f"Wrote candidates: {written}")
    typer.echo(f"Episode summary: {store.layout.episode_summary_markdown_path()}")


def run_draft_pipeline(
    *,
    dry_run: bool,
    workspace: Path,
    episode_id: str,
    transcript: Path | None,
    chapters: Path | None,
    candidates_per_asset: int,
    chunker_config: ChunkerConfig,
    summarizer_config: StubSummarizerConfig,
    timeout_seconds: float | None = None,
) -> None:
    if dry_run:
        if transcript is None:
            typer.echo("--transcript is required for --dry-run", err=True)
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
        return

    # --- Real LLM pipeline ---
    store = _open_or_create_workspace(
        workspace=workspace,
        episode_id=episode_id,
        transcript=transcript,
    )

    if transcript is not None:
        _ingest_transcript(store=store, transcript=transcript)

    if chapters is not None:
        _copy_chapters_into_workspace(workspace=workspace, chapters=chapters)

    _run_llm_pipeline(
        store=store,
        candidates_per_asset=candidates_per_asset,
        chunker_config=chunker_config,
        timeout_seconds=timeout_seconds,
    )
