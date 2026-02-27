from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import typer

from podcast_pipeline.domain.models import Candidate
from podcast_pipeline.pick_core import (
    build_asset,
    find_candidate_by_id,
    load_candidates,
    load_workspace,
    update_workspace_assets,
)
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


@dataclass(frozen=True)
class _Selection:
    asset_id: str
    candidate: Candidate
    path: Path


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
        raise typer.BadParameter(str(exc)) from exc
    if not candidates_by_asset:
        raise typer.BadParameter(f"No candidates found under {layout.copy_candidates_dir}")

    workspace_state = load_workspace(store)
    assets_by_id = {asset.asset_id: asset for asset in workspace_state.assets}

    selections: list[_Selection] = []
    for asset_key in sorted(candidates_by_asset):
        candidates = candidates_by_asset[asset_key]
        existing = assets_by_id.get(asset_key)
        chosen = _choose_candidate(
            asset_id=asset_key,
            candidates=candidates,
            candidate_id=candidate_id,
            selected_candidate_id=existing.selected_candidate_id if existing else None,
        )
        selected_path = store.write_selected_text(asset_key, chosen.format, chosen.content)
        assets_by_id[asset_key] = build_asset(
            asset_id=asset_key,
            existing=existing,
            candidates=candidates,
            selected_candidate_id=chosen.candidate_id,
        )
        workspace_state = update_workspace_assets(workspace_state, assets_by_id)
        store.write_state(workspace_state)
        selections.append(_Selection(asset_id=asset_key, candidate=chosen, path=selected_path))

    typer.echo(f"Workspace: {workspace}")
    for selection in selections:
        typer.echo(f"Selected {selection.asset_id}: {selection.path}")


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
            raise typer.BadParameter(f"candidate_id {candidate_id} not found for asset {asset_id}")
        typer.echo(f"Asset: {asset_id}")
        typer.echo(f"Selected candidate: {candidate_id}")
        return match

    typer.echo(f"Asset: {asset_id}")
    if len(candidates) == 1:
        typer.echo("Only one candidate found; selecting it.")
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
    if len(cleaned) > 72:
        cleaned = f"{cleaned[:69]}..."
    return cleaned


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
