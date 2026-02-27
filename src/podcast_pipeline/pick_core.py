from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from podcast_pipeline.domain.models import Asset, AssetKind, Candidate, EpisodeWorkspace
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore


def load_candidates(
    *,
    layout: EpisodeWorkspaceLayout,
    asset_id: str | None,
) -> dict[str, list[Candidate]]:
    """Load candidate JSON files from workspace, optionally filtered by asset_id."""
    root = layout.copy_candidates_dir
    if not root.exists():
        raise ValueError(f"Missing candidates directory: {root}")

    asset_dirs: list[Path]
    if asset_id is not None:
        validate_asset_id(asset_id)
        asset_dir = root / asset_id
        if not asset_dir.exists():
            raise ValueError(f"Missing candidates directory for asset {asset_id}: {asset_dir}")
        asset_dirs = [asset_dir]
    else:
        asset_dirs = [path for path in sorted(root.iterdir()) if path.is_dir()]

    candidates_by_asset: dict[str, list[Candidate]] = {}
    for asset_dir in asset_dirs:
        candidates = _load_candidates_from_dir(asset_dir)
        if candidates:
            candidates_by_asset[asset_dir.name] = candidates

    if asset_id is not None and asset_id not in candidates_by_asset:
        raise ValueError(f"No candidates found for asset {asset_id}")
    return candidates_by_asset


def _load_candidates_from_dir(path: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    for candidate_path in sorted(path.glob("candidate_*.json")):
        try:
            raw = json.loads(candidate_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON at {candidate_path}: {exc}") from exc
        try:
            candidate = Candidate.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"Invalid candidate schema at {candidate_path}: {exc}") from exc
        if candidate.asset_id != path.name:
            raise ValueError(f"candidate asset_id mismatch: {candidate_path}")
        candidates.append(candidate)
    candidates.sort(key=lambda item: (item.created_at, str(item.candidate_id)))
    return candidates


def load_workspace(store: EpisodeWorkspaceStore) -> EpisodeWorkspace:
    """Load workspace state, creating a default if state.json doesn't exist."""
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


def build_asset(
    *,
    asset_id: str,
    existing: Asset | None,
    candidates: list[Candidate],
    selected_candidate_id: UUID,
) -> Asset:
    """Build an Asset model with merged candidates and the selected candidate id."""
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


def update_workspace_assets(
    workspace: EpisodeWorkspace,
    assets_by_id: dict[str, Asset],
) -> EpisodeWorkspace:
    """Return a copy of workspace with merged/updated assets."""
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
    for asset_id_key in sorted(assets_by_id):
        if asset_id_key not in seen:
            out.append(assets_by_id[asset_id_key])
    return out


def find_candidate_by_id(candidates: list[Candidate], candidate_id: UUID) -> Candidate | None:
    """Find a candidate by its UUID, or return None."""
    for candidate in candidates:
        if candidate.candidate_id == candidate_id:
            return candidate
    return None


def validate_asset_id(asset_id: str) -> None:
    """Raise ValueError if asset_id contains path separators."""
    if "/" in asset_id or "\\" in asset_id:
        raise ValueError("asset_id must not contain path separators")


def _asset_kind(asset_id: str) -> AssetKind | None:
    try:
        return AssetKind(asset_id)
    except ValueError:
        return None
