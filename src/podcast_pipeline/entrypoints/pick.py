from __future__ import annotations

from pathlib import Path
from uuid import UUID

import typer

from podcast_pipeline.domain.models import Candidate
from podcast_pipeline.pick_core import (
    candidate_generation,
    find_candidate_by_id,
    load_candidates,
    load_workspace,
    save_candidate_selection,
)
from podcast_pipeline.terminal_text import terminal_text
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def run_pick(
    *,
    workspace: Path,
    asset_id: str | None,
    candidate_id: UUID | None,
) -> None:
    if candidate_id is not None and asset_id is None:
        raise typer.BadParameter("--candidate-id requires --asset-id")
    store = EpisodeWorkspaceStore(workspace)
    layout = store.layout
    try:
        candidates_by_asset = load_candidates(layout=layout, asset_id=asset_id)
    except ValueError as exc:
        raise typer.BadParameter(terminal_text(str(exc))) from exc
    if not candidates_by_asset:
        raise typer.BadParameter(f"No candidates found under {terminal_text(str(layout.copy_candidates_dir))}")

    workspace_state = load_workspace(store)
    assets_by_id = {asset.asset_id: asset for asset in workspace_state.assets}

    typer.echo(f"Workspace: {terminal_text(str(workspace))}")
    completed = 0
    total = len(candidates_by_asset)
    try:
        for asset_key in sorted(candidates_by_asset):
            candidates = candidates_by_asset[asset_key]
            generations = {c.candidate_id: candidate_generation(layout=layout, candidate=c) for c in candidates}
            existing = assets_by_id.get(asset_key)
            chosen = _choose_candidate(
                asset_id=asset_key,
                candidates=candidates,
                candidate_id=candidate_id,
                selected_candidate_id=existing.selected_candidate_id if existing else None,
            )
            workspace_state = save_candidate_selection(
                store=store,
                workspace=workspace_state,
                candidate=chosen,
                candidates=candidates,
                selected_format=chosen.format,
                selected_text=chosen.content,
                expected_generation=generations[chosen.candidate_id],
            )
            assets_by_id = {asset.asset_id: asset for asset in workspace_state.assets}
            selected_path = layout.selected_text_path(asset_key, chosen.format)
            completed += 1
            typer.echo(
                f"Selected {terminal_text(asset_key)}: {terminal_text(str(selected_path))} "
                f"(candidate={chosen.candidate_id})"
            )
    except (typer.Abort, KeyboardInterrupt, EOFError):
        typer.echo(f"Pick incomplete: {completed}/{total} assets committed. Earlier receipts remain saved.", err=True)
        raise typer.Exit(1) from None
    except Exception as exc:
        typer.echo(f"Pick incomplete: {completed}/{total} assets committed. Earlier receipts remain saved.", err=True)
        typer.echo(f"Selection stopped: {terminal_text(str(exc))}", err=True)
        raise typer.Exit(1) from None


def _choose_candidate(
    *,
    asset_id: str,
    candidates: list[Candidate],
    candidate_id: UUID | None,
    selected_candidate_id: UUID | None,
) -> Candidate:
    if candidate_id is not None:
        match = find_candidate_by_id(candidates, candidate_id)
        if match is None:
            raise typer.BadParameter(f"candidate_id {candidate_id} not found for asset {terminal_text(asset_id)}")
        typer.echo(f"Asset: {terminal_text(asset_id)}")
        typer.echo(f"Candidate: {candidate_id}")
        return match

    typer.echo(f"Asset: {terminal_text(asset_id)}")
    if len(candidates) == 1:
        typer.echo("Only one candidate found.")
        return candidates[0]

    default_index = _candidate_index(candidates, selected_candidate_id)
    if selected_candidate_id is not None and default_index is None:
        typer.echo("Previously selected candidate missing; defaulting to 1.", err=True)

    for idx, candidate in enumerate(candidates, start=1):
        marker = "*" if candidate.candidate_id == selected_candidate_id else " "
        preview = _candidate_preview(candidate)
        typer.echo(f"{marker} [{idx}] {preview} ({candidate.format.value}) {candidate.candidate_id}")

    while True:
        choice = int(
            typer.prompt(
                "Select candidate",
                type=int,
                default=default_index or 1,
            ),
        )
        if 1 <= choice <= len(candidates):
            return candidates[choice - 1]
        typer.echo(f"Enter a number between 1 and {len(candidates)}.", err=True)


def _candidate_index(candidates: list[Candidate], candidate_id: UUID | None) -> int | None:
    if candidate_id is None:
        return None
    for idx, candidate in enumerate(candidates, start=1):
        if candidate.candidate_id == candidate_id:
            return idx
    return None


def _candidate_preview(candidate: Candidate) -> str:
    first_line = _first_non_empty_line(candidate.content)
    cleaned = first_line.lstrip("#").strip() if first_line else ""
    if not cleaned:
        cleaned = "(empty)"
    return terminal_text(cleaned, limit=72)


def _first_non_empty_line(text: str) -> str:
    for line in text[:4096].split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
