from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import typer

from podcast_pipeline.domain.models import Asset, AssetKind, Candidate, EpisodeWorkspace
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore


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
    candidates_by_asset = _load_candidates(layout=layout, asset_id=asset_id)
    if not candidates_by_asset:
        raise typer.BadParameter(f"No candidates found under {layout.copy_candidates_dir}")

    workspace_state = _load_workspace(store)
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
        assets_by_id[asset_key] = _build_asset(
            asset_id=asset_key,
            existing=existing,
            candidates=candidates,
            selected_candidate_id=chosen.candidate_id,
        )
        workspace_state = _update_workspace_assets(workspace_state, assets_by_id)
        store.write_state(workspace_state)
        selections.append(_Selection(asset_id=asset_key, candidate=chosen, path=selected_path))

    typer.echo(f"Workspace: {workspace}")
    for selection in selections:
        typer.echo(f"Selected {selection.asset_id}: {selection.path}")


def _load_candidates(
    *,
    layout: EpisodeWorkspaceLayout,
    asset_id: str | None,
) -> dict[str, list[Candidate]]:
    root = layout.copy_candidates_dir
    if not root.exists():
        raise typer.BadParameter(f"Missing candidates directory: {root}")

    asset_dirs: list[Path]
    if asset_id is not None:
        _validate_asset_id(asset_id)
        asset_dir = root / asset_id
        if not asset_dir.exists():
            raise typer.BadParameter(f"Missing candidates directory for asset {asset_id}: {asset_dir}")
        asset_dirs = [asset_dir]
    else:
        asset_dirs = [path for path in sorted(root.iterdir()) if path.is_dir()]

    candidates_by_asset: dict[str, list[Candidate]] = {}
    for asset_dir in asset_dirs:
        candidates = _load_candidates_from_dir(asset_dir)
        if candidates:
            candidates_by_asset[asset_dir.name] = candidates

    if asset_id is not None and asset_id not in candidates_by_asset:
        raise typer.BadParameter(f"No candidates found for asset {asset_id}")
    return candidates_by_asset


def _load_candidates_from_dir(path: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    for candidate_path in sorted(path.glob("candidate_*.json")):
        try:
            raw = json.loads(candidate_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"Invalid JSON at {candidate_path}: {exc}") from exc
        candidate = Candidate.model_validate(raw)
        if candidate.asset_id != path.name:
            raise typer.BadParameter(f"candidate asset_id mismatch: {candidate_path}")
        candidates.append(candidate)
    candidates.sort(key=lambda item: (item.created_at, str(item.candidate_id)))
    return candidates


def _choose_candidate(
    *,
    asset_id: str,
    candidates: list[Candidate],
    candidate_id: UUID | None,
    selected_candidate_id: UUID | None,
) -> Candidate:
    if candidate_id is not None:
        match = _find_candidate_by_id(candidates, candidate_id)
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


def _find_candidate_by_id(candidates: list[Candidate], candidate_id: UUID) -> Candidate | None:
    for candidate in candidates:
        if candidate.candidate_id == candidate_id:
            return candidate
    return None


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


def _build_asset(
    *,
    asset_id: str,
    existing: Asset | None,
    candidates: list[Candidate],
    selected_candidate_id: UUID,
) -> Asset:
    merged_candidates = _merge_candidates(existing, candidates)
    reviews = list(existing.reviews) if existing else []
    kind = existing.kind if existing and existing.kind is not None else _asset_kind(asset_id)
    return Asset(
        asset_id=asset_id,
        kind=kind,
        candidates=merged_candidates,
        reviews=reviews,
        selected_candidate_id=selected_candidate_id,
    )


def _merge_candidates(existing: Asset | None, candidates: list[Candidate]) -> list[Candidate]:
    by_id: dict[UUID, Candidate] = {}
    if existing is not None:
        for candidate in existing.candidates:
            by_id[candidate.candidate_id] = candidate
    for candidate in candidates:
        by_id[candidate.candidate_id] = candidate

    merged: list[Candidate] = []
    seen: set[UUID] = set()
    for candidate in candidates:
        merged.append(by_id[candidate.candidate_id])
        seen.add(candidate.candidate_id)
    if existing is not None:
        for candidate in existing.candidates:
            if candidate.candidate_id not in seen:
                merged.append(candidate)
    return merged


def _update_workspace_assets(
    workspace: EpisodeWorkspace,
    assets_by_id: dict[str, Asset],
) -> EpisodeWorkspace:
    updated_assets = _merge_assets(workspace.assets, assets_by_id)
    return workspace.model_copy(update={"assets": updated_assets})


def _merge_assets(existing: list[Asset], assets_by_id: dict[str, Asset]) -> list[Asset]:
    out: list[Asset] = []
    seen: set[str] = set()
    for asset in existing:
        replacement = assets_by_id.get(asset.asset_id)
        if replacement is not None:
            out.append(replacement)
        else:
            out.append(asset)
        seen.add(asset.asset_id)
    for asset_id in sorted(assets_by_id):
        if asset_id not in seen:
            out.append(assets_by_id[asset_id])
    return out


def _load_workspace(store: EpisodeWorkspaceStore) -> EpisodeWorkspace:
    if store.layout.state_json.exists():
        return store.read_state()
    episode_id = _episode_id_from_yaml(store)
    return EpisodeWorkspace(episode_id=episode_id, root_dir=".")


def _episode_id_from_yaml(store: EpisodeWorkspaceStore) -> str:
    if store.layout.episode_yaml.exists():
        data = store.read_episode_yaml()
        episode_id = data.get("episode_id")
        if isinstance(episode_id, str) and episode_id.strip():
            return episode_id
    return store.layout.root.name


def _asset_kind(asset_id: str) -> AssetKind | None:
    try:
        return AssetKind(asset_id)
    except ValueError:
        return None


def _validate_asset_id(asset_id: str) -> None:
    if "/" in asset_id or "\\" in asset_id:
        raise typer.BadParameter("asset_id must not contain path separators")
