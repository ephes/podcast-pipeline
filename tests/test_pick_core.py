from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from podcast_pipeline.domain.models import Asset, Candidate, EpisodeWorkspace
from podcast_pipeline.pick_core import (
    build_asset,
    find_candidate_by_id,
    load_candidates,
    load_workspace,
    update_workspace_assets,
    validate_asset_id,
)
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore


def _write_candidate(layout: EpisodeWorkspaceLayout, asset_id: str, content: str) -> Candidate:
    """Write a candidate JSON file and return the model."""
    candidate = Candidate(asset_id=asset_id, content=content)
    store = EpisodeWorkspaceStore(layout.root)
    store.write_candidate(candidate)
    return candidate


def test_load_candidates_single_asset(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    c1 = _write_candidate(layout, "description", "Candidate 1")
    c2 = _write_candidate(layout, "description", "Candidate 2")

    result = load_candidates(layout=layout, asset_id="description")
    assert "description" in result
    assert len(result["description"]) == 2
    ids = {c.candidate_id for c in result["description"]}
    assert c1.candidate_id in ids
    assert c2.candidate_id in ids


def test_load_candidates_all_assets(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    _write_candidate(layout, "description", "Desc")
    _write_candidate(layout, "shownotes", "Notes")

    result = load_candidates(layout=layout, asset_id=None)
    assert "description" in result
    assert "shownotes" in result


def test_load_candidates_missing_dir(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    with pytest.raises(ValueError, match="Missing candidates directory"):
        load_candidates(layout=layout, asset_id=None)


def test_load_candidates_no_candidates_for_asset(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    # Create the candidates dir but no asset subdirs
    layout.copy_candidates_dir.mkdir(parents=True)
    (layout.copy_candidates_dir / "description").mkdir()

    with pytest.raises(ValueError, match="No candidates found for asset"):
        load_candidates(layout=layout, asset_id="description")


def test_load_workspace_from_state_json(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    ws = EpisodeWorkspace(episode_id="ep_test", root_dir=".")
    store.write_state(ws)

    result = load_workspace(store)
    assert result.episode_id == "ep_test"


def test_load_workspace_from_episode_yaml(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep_from_yaml"})

    result = load_workspace(store)
    assert result.episode_id == "ep_from_yaml"


def test_load_workspace_fallback_to_dirname(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    result = load_workspace(store)
    assert result.episode_id == tmp_path.name


def test_find_candidate_by_id_found() -> None:
    c1 = Candidate(asset_id="desc", content="a")
    c2 = Candidate(asset_id="desc", content="b")
    result = find_candidate_by_id([c1, c2], c2.candidate_id)
    assert result is c2


def test_find_candidate_by_id_not_found() -> None:
    c1 = Candidate(asset_id="desc", content="a")
    result = find_candidate_by_id([c1], uuid4())
    assert result is None


def test_build_asset_creates_new() -> None:
    c1 = Candidate(asset_id="description", content="test")
    asset = build_asset(
        asset_id="description",
        existing=None,
        candidates=[c1],
        selected_candidate_id=c1.candidate_id,
    )
    assert asset.asset_id == "description"
    assert asset.selected_candidate_id == c1.candidate_id
    assert len(asset.candidates) == 1


def test_build_asset_merges_existing() -> None:
    c_old = Candidate(asset_id="description", content="old")
    c_new = Candidate(asset_id="description", content="new")
    existing = Asset(
        asset_id="description",
        candidates=[c_old],
        selected_candidate_id=c_old.candidate_id,
    )
    asset = build_asset(
        asset_id="description",
        existing=existing,
        candidates=[c_new],
        selected_candidate_id=c_new.candidate_id,
    )
    assert asset.selected_candidate_id == c_new.candidate_id
    assert len(asset.candidates) == 2


def test_update_workspace_assets_adds_new() -> None:
    ws = EpisodeWorkspace(episode_id="ep", root_dir=".")
    c = Candidate(asset_id="description", content="test")
    new_asset = Asset(
        asset_id="description",
        candidates=[c],
        selected_candidate_id=c.candidate_id,
    )
    updated = update_workspace_assets(ws, {"description": new_asset})
    assert len(updated.assets) == 1
    assert updated.assets[0].asset_id == "description"


def test_update_workspace_assets_replaces_existing() -> None:
    c1 = Candidate(asset_id="description", content="old")
    old_asset = Asset(asset_id="description", candidates=[c1], selected_candidate_id=c1.candidate_id)
    ws = EpisodeWorkspace(episode_id="ep", root_dir=".", assets=[old_asset])

    c2 = Candidate(asset_id="description", content="new")
    new_asset = Asset(asset_id="description", candidates=[c2], selected_candidate_id=c2.candidate_id)
    updated = update_workspace_assets(ws, {"description": new_asset})
    assert len(updated.assets) == 1
    assert updated.assets[0].selected_candidate_id == c2.candidate_id


def test_validate_asset_id_rejects_slashes() -> None:
    with pytest.raises(ValueError, match="path separators"):
        validate_asset_id("foo/bar")
    with pytest.raises(ValueError, match="path separators"):
        validate_asset_id("foo\\bar")


def test_validate_asset_id_accepts_valid() -> None:
    validate_asset_id("description")
    validate_asset_id("summary_short")
