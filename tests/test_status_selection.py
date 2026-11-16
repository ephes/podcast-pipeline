from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace, TextFormat
from podcast_pipeline.entrypoints import status
from podcast_pipeline.entrypoints.cli import app
from podcast_pipeline.pick_core import custom_selection_format, save_candidate_selection, save_custom_selection
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


@pytest.mark.parametrize("damage", ["extension", "empty", "stale_text", "unrecorded", "state_missing", "extra_format"])
def test_status_requires_selected_artifact_state_agreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    monkeypatch.setattr(status, "_required_asset_ids", lambda: ("description",))
    store = EpisodeWorkspaceStore(tmp_path)
    candidate = Candidate(asset_id="description", format=TextFormat.markdown, content="Synthetic selected copy")
    save_candidate_selection(
        store=store,
        workspace=EpisodeWorkspace(episode_id="synthetic", root_dir="."),
        candidate=candidate,
        candidates=[candidate],
        selected_format=candidate.format,
        selected_text=candidate.content,
    )
    path = store.layout.selected_text_path("description", TextFormat.markdown)
    if damage == "extension":
        path.rename(path.with_suffix(".txt"))
    elif damage == "empty":
        path.write_text("")
    elif damage == "stale_text":
        path.write_text("Different candidate\n")
    elif damage == "unrecorded":
        store.write_state(EpisodeWorkspace(episode_id="synthetic", root_dir="."))
    elif damage == "state_missing":
        store.layout.state_json.unlink()
    if damage == "extra_format":
        path.with_suffix(".txt").write_text("Superseded plain text")
    result = CliRunner().invoke(app, ["status", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "copy/selected: ok" not in result.output
    assert "copy/selected: missing" in result.output


@pytest.mark.parametrize("asset_id", ["description", "title_seo"])
@pytest.mark.parametrize("custom", [False, True])
def test_status_accepts_real_candidate_and_custom_selections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custom: bool, asset_id: str
) -> None:
    monkeypatch.setattr(status, "_required_asset_ids", lambda: (asset_id,))
    store = EpisodeWorkspaceStore(tmp_path)
    candidate = Candidate(
        asset_id=asset_id, format=custom_selection_format(asset_id), content="Synthetic selected copy"
    )
    workspace = EpisodeWorkspace(episode_id="synthetic", root_dir=".")
    if custom:
        save_custom_selection(store=store, workspace=workspace, asset_id=asset_id, candidates=[], text="Custom copy")
    else:
        save_candidate_selection(
            store=store,
            workspace=workspace,
            candidate=candidate,
            candidates=[candidate],
            selected_format=candidate.format,
            selected_text=candidate.content,
        )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = CliRunner().invoke(app, ["status", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "copy/selected: ok" in result.output
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
