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
        if hit and Path(source).suffix == ".tmp":
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


@pytest.mark.parametrize(
    ("failure", "selected"),
    [
        (failure, selected)
        for failure in ("json", "md", "html", "txt", "state-before", "state-after")
        for selected in (False, True)
    ]
    + [("selected-md", True), ("selected-html", True)],
)
def test_candidate_delete_failure_restores_entire_disk_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: bool, failure: str
) -> None:
    import podcast_pipeline.workspace_store as module
    from podcast_pipeline.dashboard_context import DashboardContext

    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "synthetic"})
    removed = Candidate(asset_id="description", content="Delete this copy")
    kept = Candidate(asset_id="description", content="Keep this copy")
    for candidate in (removed, kept):
        store.write_candidate(candidate)
    # Include a historical rendering in a superseded format in the compensation set.
    store.layout.candidate_text_path("description", removed.candidate_id, TextFormat.plain).write_text("Old rendering")
    ctx = DashboardContext(workspace=tmp_path)
    assert ctx.select_candidate("description", str((removed if selected else kept).candidate_id)) is None
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    paths = {
        "json": store.layout.candidate_json_path("description", removed.candidate_id),
        "md": store.layout.candidate_text_path("description", removed.candidate_id, TextFormat.markdown),
        "html": store.layout.candidate_text_path("description", removed.candidate_id, TextFormat.html),
        "txt": store.layout.candidate_text_path("description", removed.candidate_id, TextFormat.plain),
        "selected-md": store.layout.selected_text_path("description", TextFormat.markdown),
        "selected-html": store.layout.selected_text_path("description", TextFormat.html),
    }
    unlink, replace = Path.unlink, module.os.replace
    injected = False

    def fail_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal injected
        if not injected and path == paths.get(failure):
            injected = True
            raise OSError("synthetic unlink failure")
        unlink(path, *args, **kwargs)

    def fail_state(source: Path, target: Path) -> None:
        nonlocal injected
        if not injected and Path(target) == store.layout.state_json:
            assert not paths["json"].exists()  # exercise a failure after candidate retirement
            injected = True
            if failure == "state-after":
                replace(source, target)
            raise OSError("synthetic state failure")
        replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_unlink)
        if failure.startswith("state-"):
            patch.setattr(module.os, "replace", fail_state)
        with pytest.raises((OSError, WorkspaceStoreError)):
            ctx.delete_candidate("description", str(removed.candidate_id))
    assert injected
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    reopened = EpisodeWorkspaceStore(tmp_path)
    assert reopened.read_candidate("description", removed.candidate_id) == removed
    assert reopened.read_candidate("description", kept.candidate_id) == kept
    assert reopened.read_state().assets[0].selected_candidate_id == (removed if selected else kept).candidate_id
    assert ctx.delete_candidate("description", str(removed.candidate_id)) is None
    assert not paths["json"].exists()
    assert reopened.read_candidate("description", kept.candidate_id) == kept


def test_candidate_delete_retains_backup_and_reports_incomplete_compensation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import podcast_pipeline.workspace_store as module
    from podcast_pipeline.dashboard_context import DashboardContext

    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "synthetic"})
    candidate = Candidate(asset_id="description", content="Recover this copy")
    path = store.write_candidate(candidate)
    original = path.read_bytes()
    ctx = DashboardContext(workspace=tmp_path)
    assert ctx.select_candidate("description", str(candidate.candidate_id)) is None
    replace = module.os.replace

    def fail_publication_and_one_restore(source: Path, target: Path) -> None:
        if Path(target) == store.layout.state_json and Path(source).suffix == ".tmp":
            raise OSError("synthetic state failure")
        if Path(target) == path and ".restore-" in Path(source).name:
            raise OSError("synthetic restore failure")
        replace(source, target)

    monkeypatch.setattr(module.os, "replace", fail_publication_and_one_restore)
    with pytest.raises(WorkspaceStoreError, match="Artifact recovery failed") as error:
        ctx.delete_candidate("description", str(candidate.candidate_id))
    (backup,) = list(path.parent.glob(f".{path.name}.backup-*"))
    assert str(backup) in str(error.value)
    assert backup.read_bytes() == original
    assert store.read_state().assets[0].selected_candidate_id == candidate.candidate_id
    assert store.read_selected_text("description", TextFormat.markdown) == candidate.content + "\n"
