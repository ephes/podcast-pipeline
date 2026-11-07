from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from podcast_pipeline.dashboard_context import DashboardContext
from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace, TextFormat
from podcast_pipeline.entrypoints.pick_web import _ServerContext
from podcast_pipeline.pick_core import (
    has_manual_selection_provenance,
    load_workspace,
    save_custom_selection,
    write_selected_tags,
)
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def _custom_process(root: str, asset: str, barrier: Any) -> None:
    store = EpisodeWorkspaceStore(Path(root))
    stale = load_workspace(store)
    barrier.wait(10)
    save_custom_selection(store=store, workspace=stale, asset_id=asset, candidates=[], text=f"synthetic {asset}")


def test_independent_process_selections_survive(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_state(EpisodeWorkspace(episode_id="synthetic", root_dir="."))
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    workers = [
        context.Process(target=_custom_process, args=(str(tmp_path), asset, barrier))
        for asset in ["title_detail", "description"]
    ]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join(15)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
    state = store.read_state()
    for asset in ["title_detail", "description"]:
        assert has_manual_selection_provenance(state, asset)
        assert any(item.asset_id == asset for item in state.assets)


def test_stale_stores_preserve_other_selections_and_paid_history(tmp_path: Path) -> None:
    first, second = EpisodeWorkspaceStore(tmp_path), EpisodeWorkspaceStore(tmp_path)
    initial = EpisodeWorkspace(episode_id="synthetic", root_dir=".")
    first.write_state(initial)
    stale = second.read_state()
    first.set_auphonic_production_uuid("synthetic-production", default=initial)
    save_custom_selection(store=first, workspace=initial, asset_id="title_detail", candidates=[], text="First")
    updated = save_custom_selection(
        store=second, workspace=stale, asset_id="description", candidates=[], text="Second"
    )
    assert updated.auphonic_production_uuid == "synthetic-production"
    assert {asset.asset_id for asset in updated.assets} == {"title_detail", "description"}
    assert all(has_manual_selection_provenance(updated, asset) for asset in ["title_detail", "description"])
    assert first.layout.selected_text_path("description", TextFormat.markdown).read_text() == "Second\n"
    # A later same-asset writer wins and updates both metadata and artifact provenance.
    save_custom_selection(store=first, workspace=initial, asset_id="description", candidates=[], text="Last")
    assert first.layout.selected_text_path("description", TextFormat.markdown).read_text() == "Last\n"
    assert has_manual_selection_provenance(first.read_state(), "title_detail")


def test_dashboard_and_pick_candidate_writes_preserve_stale_other_assets(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "synthetic"})
    candidates = {
        asset: [Candidate(asset_id=asset, content=f"Candidate {asset}", format=TextFormat.plain)]
        for asset in ["title_detail", "summary_short"]
    }
    for values in candidates.values():
        store.write_candidate(values[0])
    dashboard = DashboardContext(workspace=tmp_path)
    pick = _ServerContext(
        store=EpisodeWorkspaceStore(tmp_path), candidates_by_asset=candidates, workspace_state=load_workspace(store)
    )
    dashboard._ensure_workspace_state()
    assert dashboard.select_custom_text("title_detail", "Custom title", None) is None
    candidate = candidates["summary_short"][0]
    assert pick.select_candidate("summary_short", str(candidate.candidate_id)) is None
    assert has_manual_selection_provenance(store.read_state(), "title_detail")
    candidate = candidates["title_detail"][0]
    assert dashboard.select_candidate("title_detail", str(candidate.candidate_id)) is None
    state = store.read_state()
    assert not has_manual_selection_provenance(state, "title_detail")
    assert all(asset.selected_candidate_id is not None for asset in state.assets)
    for values in candidates.values():
        chosen = values[0]
        assert store.layout.selected_text_path(chosen.asset_id, chosen.format).read_text().strip() == chosen.content


def test_tag_write_preserves_stale_other_selection(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "synthetic"})
    candidate = Candidate(asset_id="cms_tags", content="python", format=TextFormat.markdown)
    store.write_candidate(candidate)
    dashboard = DashboardContext(workspace=tmp_path)
    assert dashboard.select_candidate("cms_tags", str(candidate.candidate_id)) is None
    stale = store.read_state()
    save_custom_selection(store=store, workspace=stale, asset_id="title_detail", candidates=[], text="Preserve me")
    updated, error = write_selected_tags(
        store=EpisodeWorkspaceStore(tmp_path),
        workspace_state=stale,
        asset_id="cms_tags",
        tags=["synthetic"],
        max_tags=12,
    )
    assert error is None
    assert has_manual_selection_provenance(updated, "title_detail")
    assert next(a for a in updated.assets if a.asset_id == "cms_tags").selected_candidate_id is None


def test_same_asset_artifacts_and_metadata_share_lock(tmp_path: Path, monkeypatch: Any) -> None:
    import threading

    stores = [EpisodeWorkspaceStore(tmp_path), EpisodeWorkspaceStore(tmp_path)]
    initial = EpisodeWorkspace(episode_id="synthetic", root_dir=".")
    stores[0].write_state(initial)
    candidates = [
        Candidate(asset_id="title_detail", format=TextFormat.plain, content=text) for text in ["First", "Second"]
    ]
    contexts = [
        _ServerContext(store=store, candidates_by_asset={"title_detail": candidates}, workspace_state=initial)
        for store in stores
    ]
    published, release, second_done = threading.Event(), threading.Event(), threading.Event()
    original = stores[0].write_selected_text

    def pause(*args: Any, **kwargs: Any) -> Path:
        path = original(*args, **kwargs)
        published.set()
        assert release.wait(5)
        return path

    monkeypatch.setattr(stores[0], "write_selected_text", pause)
    errors: list[str | None] = []

    def select(index: int) -> None:
        errors.append(contexts[index].select_candidate("title_detail", str(candidates[index].candidate_id)))
        if index == 1:
            second_done.set()

    first, second = threading.Thread(target=select, args=(0,)), threading.Thread(target=select, args=(1,))
    first.start()
    assert published.wait(5)
    second.start()
    try:
        second_done.wait(0.2)
    finally:
        release.set()
        first.join(5)
        second.join(5)
    assert errors == [None, None]
    selected = stores[0].read_state().assets[0].selected_candidate_id
    text = stores[0].layout.selected_text_path("title_detail", TextFormat.plain).read_text().strip()
    assert text == next(candidate.content for candidate in candidates if candidate.candidate_id == selected)


def test_stale_dashboard_delete_preserves_newer_selections(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "synthetic"})
    candidates = [Candidate(asset_id="description", content=text) for text in ["Old", "New"]]
    for candidate in candidates:
        store.write_candidate(candidate)
    dashboard = DashboardContext(workspace=tmp_path)
    assert dashboard.select_candidate("description", str(candidates[0].candidate_id)) is None
    other = _ServerContext(
        store=EpisodeWorkspaceStore(tmp_path),
        candidates_by_asset={"description": candidates},
        workspace_state=store.read_state(),
    )
    assert other.select_candidate("description", str(candidates[1].candidate_id)) is None
    save_custom_selection(
        store=store, workspace=store.read_state(), asset_id="title_detail", candidates=[], text="Keep"
    )
    assert dashboard.delete_candidate("description", str(candidates[0].candidate_id)) is None
    current = store.read_state()
    assert has_manual_selection_provenance(current, "title_detail")
    assert (
        next(a for a in current.assets if a.asset_id == "description").selected_candidate_id
        == candidates[1].candidate_id
    )
    assert store.layout.selected_text_path("description", candidates[1].format).read_text().strip() == "New"


@pytest.mark.parametrize("concurrent_asset", ["title_detail", "description"])
def test_review_completion_preserves_concurrent_selection(
    tmp_path: Path,
    monkeypatch: Any,
    concurrent_asset: str,
) -> None:
    import threading

    from podcast_pipeline.agent_runners import FakeCreatorRunner, FakeReviewerRunner
    from podcast_pipeline.review_loop_orchestrator import run_review_loop_orchestrator

    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "synthetic"})
    initial = EpisodeWorkspace(episode_id="synthetic", root_dir=".")
    store.write_state(initial)
    read, release, selection_done = threading.Event(), threading.Event(), threading.Event()
    original = EpisodeWorkspaceStore.read_state

    def pause_after_read(self: EpisodeWorkspaceStore) -> EpisodeWorkspace:
        current = original(self)
        if threading.current_thread().name == "synthetic-review":
            read.set()
            assert release.wait(5)
        return current

    monkeypatch.setattr(EpisodeWorkspaceStore, "read_state", pause_after_read)
    failures: list[BaseException] = []

    def review() -> None:
        try:
            run_review_loop_orchestrator(
                workspace=tmp_path,
                asset_id="description",
                max_iterations=1,
                creator=FakeCreatorRunner(
                    layout=store.layout, replies=[{"done": True, "candidate": {"content": "Reviewed"}}]
                ),
                reviewer=FakeReviewerRunner(layout=store.layout, reviewer="synthetic", replies=[{"verdict": "ok"}]),
            )
        except BaseException as exc:
            failures.append(exc)

    def select() -> None:
        try:
            save_custom_selection(
                store=EpisodeWorkspaceStore(tmp_path),
                workspace=initial,
                asset_id=concurrent_asset,
                candidates=[],
                text="Concurrent",
            )
        except BaseException as exc:
            failures.append(exc)
        finally:
            selection_done.set()

    first = threading.Thread(target=review, name="synthetic-review")
    second = threading.Thread(target=select)
    first.start()
    assert read.wait(5)
    second.start()
    try:
        selection_done.wait(0.2)
    finally:
        release.set()
        first.join(5)
        second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not failures
    current = store.read_state()
    assert has_manual_selection_provenance(current, concurrent_asset)
    description = next(a for a in current.assets if a.asset_id == "description")
    if concurrent_asset == "description":
        assert description.selected_candidate_id is None
        assert store.layout.selected_text_path("description", TextFormat.markdown).read_text().strip() == "Concurrent"
    else:
        assert description.selected_candidate_id is not None
        assert store.layout.selected_text_path("description", TextFormat.markdown).read_text().strip() == "Reviewed"


def test_nonconverged_review_keeps_published_candidate(tmp_path: Path) -> None:
    from podcast_pipeline.agent_runners import FakeCreatorRunner, FakeReviewerRunner
    from podcast_pipeline.review_loop_orchestrator import run_review_loop_orchestrator

    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "synthetic"})
    candidate = Candidate(asset_id="description", content="Selected")
    store.write_candidate(candidate)
    dashboard = DashboardContext(workspace=tmp_path)
    assert dashboard.select_candidate("description", str(candidate.candidate_id)) is None
    run_review_loop_orchestrator(
        workspace=tmp_path,
        asset_id="description",
        max_iterations=1,
        creator=FakeCreatorRunner(
            layout=store.layout, replies=[{"done": False, "candidate": {"content": "Unfinished"}}]
        ),
        reviewer=FakeReviewerRunner(
            layout=store.layout, reviewer="synthetic", replies=[{"verdict": "changes_requested"}]
        ),
    )
    selected = store.read_state().assets[0]
    assert selected.selected_candidate_id == candidate.candidate_id
    assert any(c.candidate_id == candidate.candidate_id for c in selected.candidates)
    assert store.layout.selected_text_path("description", candidate.format).read_text().strip() == "Selected"
