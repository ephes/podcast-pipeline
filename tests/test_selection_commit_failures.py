from pathlib import Path
from typing import Any

import pytest

from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace, TextFormat
from podcast_pipeline.pick_core import save_candidate_selection, save_custom_selection
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore, WorkspaceStoreError


@pytest.mark.parametrize("invalid", [b"{broken", b'{"episode_id": 42}', b"\xff"])
def test_stale_writer_refuses_invalid_existing_paid_state(tmp_path: Path, invalid: bytes) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_state(EpisodeWorkspace(episode_id="synthetic", root_dir=".", auphonic_production_uuid="paid-history"))
    stale = EpisodeWorkspace(episode_id="synthetic", root_dir=".")
    store.layout.state_json.write_bytes(invalid)
    before = store.layout.state_json.read_bytes()
    with pytest.raises((WorkspaceStoreError, ValueError)):
        store.write_state(stale)
    assert store.layout.state_json.read_bytes() == before


def test_unreadable_state_is_not_treated_as_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    initial = EpisodeWorkspace(episode_id="synthetic", root_dir=".")
    store.write_state(initial)
    before = store.layout.state_json.read_bytes()

    def denied() -> EpisodeWorkspace:
        raise PermissionError("synthetic read denial")

    monkeypatch.setattr(store, "read_state", denied)
    with pytest.raises(PermissionError):
        store.write_state(initial)
    assert store.layout.state_json.read_bytes() == before


@pytest.mark.parametrize(
    ("manual", "failure"),
    [(False, "state"), (True, "state"), (True, "provenance"), (False, "serialize"), (True, "serialize")],
)
def test_selection_failure_restores_text_provenance_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manual: bool, failure: str
) -> None:
    import podcast_pipeline.workspace_store as module

    store = EpisodeWorkspaceStore(tmp_path)
    initial = EpisodeWorkspace(episode_id="synthetic", root_dir=".")
    store.write_state(initial)
    save_custom_selection(store=store, workspace=initial, asset_id="description", candidates=[], text="old selection")
    candidate = Candidate(asset_id="description", format=TextFormat.markdown, content="new selection")

    def select() -> None:
        if manual:
            save_custom_selection(
                store=store, workspace=initial, asset_id="description", candidates=[], text="new selection"
            )
        else:
            save_candidate_selection(
                store=store,
                workspace=initial,
                candidate=candidate,
                candidates=[candidate],
                selected_format=candidate.format,
                selected_text=candidate.content,
            )

    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    injected: list[object] = []
    replace = module.os.replace
    dumps = module.json.dumps

    def fail_replace(source: Path, target: Path) -> None:
        target = Path(target)
        hit = target == store.layout.state_json if failure == "state" else "provenance" in target.parts
        if hit and ".backup-" not in Path(source).name:
            injected.append(target)
            raise OSError("synthetic publication failure")
        replace(source, target)

    def fail_dumps(value: object, *args: Any, **kwargs: Any) -> str:
        if isinstance(value, dict) and "episode_id" in value:
            injected.append("serialize")
            raise ValueError("synthetic state serialization failure")
        return dumps(value, *args, **kwargs)

    with monkeypatch.context() as patch:
        if failure == "serialize":
            patch.setattr(module.json, "dumps", fail_dumps)
        else:
            patch.setattr(module.os, "replace", fail_replace)
        with pytest.raises((OSError, ValueError)):
            select()
    assert injected
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    select()
    assert store.layout.selected_text_path("description", TextFormat.markdown).read_text().strip() == "new selection"
    if not manual:
        assert store.read_state().assets[0].selected_candidate_id == candidate.candidate_id
