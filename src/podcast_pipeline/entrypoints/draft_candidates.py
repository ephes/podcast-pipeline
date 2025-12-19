from __future__ import annotations

import json
from pathlib import Path

import typer

from podcast_pipeline.asset_candidates_stub import (
    DraftCandidatesConfig,
    generate_draft_candidates,
)
from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


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


def _load_chapters(*, store: EpisodeWorkspaceStore, chapters: Path | None) -> list[str]:
    if chapters is not None:
        return _first_non_empty_lines(chapters.read_text(encoding="utf-8"), limit=200)

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


def run_draft_candidates(
    *,
    workspace: Path,
    chapters: Path | None,
    candidates_per_asset: int,
) -> None:
    store = EpisodeWorkspaceStore(workspace)
    summary_path = store.layout.episode_summary_json_path()
    if not summary_path.exists():
        raise typer.BadParameter(f"Missing episode summary JSON: {summary_path}")

    episode_summary = EpisodeSummary.model_validate(
        json.loads(summary_path.read_text(encoding="utf-8")),
    )
    chapters_lines = _load_chapters(store=store, chapters=chapters)

    assets = generate_draft_candidates(
        episode_summary=episode_summary,
        chapters=chapters_lines,
        config=DraftCandidatesConfig(candidates_per_asset=candidates_per_asset),
    )

    written = 0
    for _asset_id, candidates in sorted(assets.items()):
        for candidate in candidates:
            store.write_candidate(candidate)
            written += 1

    typer.echo(f"Workspace: {workspace}")
    typer.echo(f"Wrote candidates: {written}")
