from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import typer

from podcast_pipeline.asset_candidates_stub import (
    DraftCandidatesConfig,
    generate_draft_candidates,
    validate_candidate_count,
)
from podcast_pipeline.file_inputs import read_regular_utf8, read_workspace_utf8
from podcast_pipeline.summary_inputs import EpisodeSummaryInputError, load_workspace_episode_summary
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore

MAX_CHAPTER_BYTES = 1024 * 1024
MAX_CHAPTER_LINES = 200


def _first_non_empty_lines(text: str, *, limit: int) -> list[str]:
    physical_lines = text.splitlines()
    if len(physical_lines) > limit:
        raise typer.BadParameter("Chapters exceed the 200 physical line limit")
    lines: list[str] = []
    for raw in physical_lines:
        stripped = raw.strip()
        if not stripped:
            continue
        lines.append(stripped)
        if len(lines) >= limit:
            break
    return lines


def _load_chapters(*, store: EpisodeWorkspaceStore, chapters: Path | None) -> list[str]:
    try:
        if chapters is not None:
            text = read_regular_utf8(chapters, max_bytes=MAX_CHAPTER_BYTES)
        else:
            episode_yaml = store.read_episode_yaml()
            raw_inputs = episode_yaml.get("inputs")
            reference = raw_inputs.get("chapters") if isinstance(raw_inputs, dict) else None
            if not isinstance(reference, str):
                reference = "transcript/chapters.txt"
            try:
                text = read_workspace_utf8(store.layout.root, Path(reference), max_bytes=MAX_CHAPTER_BYTES)
            except FileNotFoundError:
                return []
        return _first_non_empty_lines(text, limit=MAX_CHAPTER_LINES)
    except (OSError, ValueError):
        raise typer.BadParameter(
            "Invalid chapters: require coherent regular UTF-8 input within 1 MiB/200 lines"
        ) from None


def run_draft_candidates(
    *,
    workspace: Path,
    chapters: Path | None,
    candidates_per_asset: int,
) -> None:
    validate_candidate_count(candidates_per_asset)
    store = EpisodeWorkspaceStore(workspace)
    try:
        episode_summary = load_workspace_episode_summary(store.layout)
    except FileNotFoundError:
        raise typer.BadParameter(f"Missing episode summary JSON: {store.layout.episode_summary_json_path()}") from None
    except EpisodeSummaryInputError as exc:
        raise typer.BadParameter(str(exc)) from None
    chapters_lines = _load_chapters(store=store, chapters=chapters)

    assets = generate_draft_candidates(
        episode_summary=episode_summary,
        chapters=chapters_lines,
        config=DraftCandidatesConfig(candidates_per_asset=candidates_per_asset),
    )

    confirmed: list[str] = []
    try:
        for _asset_id, candidates in sorted(assets.items()):
            for candidate in candidates:
                store.write_candidate(candidate, immutable=True)
                confirmed.append(str(candidate.candidate_id))
    except Exception:
        # A failure to report progress must preserve the publication error.
        with suppress(Exception):
            typer.echo("\n".join([f"Confirmed candidates: {len(confirmed)}", *confirmed]), err=True)
        raise

    typer.echo(f"Workspace: {workspace}")
    typer.echo(f"Wrote candidates: {len(confirmed)}")
