from __future__ import annotations

from pathlib import Path

from podcast_pipeline.agent_runners import FakeCreatorRunner, FakeReviewerRunner
from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace, ReviewVerdict, TextFormat
from podcast_pipeline.review_loop_engine import LoopOutcome
from podcast_pipeline.review_loop_orchestrator import run_review_loop_orchestrator
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def _init_workspace(tmp_path: Path) -> EpisodeWorkspaceStore:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep_001", "inputs": {}})
    store.write_state(EpisodeWorkspace(episode_id="ep_001", root_dir="."))
    return store


def test_orchestrator_runs_with_seed_and_writes_protocol_files(tmp_path: Path) -> None:
    store = _init_workspace(tmp_path)
    seed_candidate = Candidate(asset_id="description", content="seed")
    store.write_candidate(seed_candidate)

    creator = FakeCreatorRunner(
        layout=store.layout,
        replies=[{"done": True, "candidate": {"content": "final copy"}}],
    )
    reviewer = FakeReviewerRunner(
        layout=store.layout,
        reviewer="reviewer_a",
        replies=[{"verdict": "ok"}],
    )

    protocol_state = run_review_loop_orchestrator(
        workspace=store.layout.root,
        asset_id="description",
        max_iterations=3,
        creator=creator,
        reviewer=reviewer,
    )

    assert protocol_state.decision is not None
    assert protocol_state.decision.outcome == LoopOutcome.converged
    assert len(protocol_state.iterations) == 1
    assert store.layout.protocol_iteration_json_path("description", 1).exists()
    assert store.layout.protocol_state_json_path("description").exists()
    assert store.layout.review_iteration_json_path("description", 1, reviewer="reviewer_a").exists()
    assert store.layout.selected_text_path("description", protocol_state.iterations[-1].candidate.format).exists()

    assert creator.calls
    assert creator.calls[0].previous_candidate is not None
    assert creator.calls[0].previous_candidate.candidate_id == seed_candidate.candidate_id


def test_orchestrator_stops_at_iteration_limit(tmp_path: Path) -> None:
    store = _init_workspace(tmp_path)
    seed_candidate = Candidate(asset_id="description", content="seed")
    store.write_candidate(seed_candidate)

    creator = FakeCreatorRunner(
        layout=store.layout,
        replies=[
            {"done": False, "candidate": {"content": "draft 1"}},
            {"done": False, "candidate": {"content": "draft 2"}},
        ],
    )
    reviewer = FakeReviewerRunner(
        layout=store.layout,
        reviewer="reviewer_a",
        replies=[
            {"verdict": "changes_requested", "issues": [{"message": "add more detail"}]},
            {"verdict": "changes_requested", "issues": [{"message": "still needs edits"}]},
        ],
    )

    protocol_state = run_review_loop_orchestrator(
        workspace=store.layout.root,
        asset_id="description",
        max_iterations=2,
        creator=creator,
        reviewer=reviewer,
    )

    assert protocol_state.decision is not None
    assert protocol_state.decision.outcome == LoopOutcome.needs_human
    assert len(protocol_state.iterations) == 2
    assert store.layout.protocol_iteration_json_path("description", 2).exists()

    workspace_state = store.read_state()
    asset = next(asset for asset in workspace_state.assets if asset.asset_id == "description")
    assert asset.selected_candidate_id is None


def test_orchestrator_rejects_locked_selection_changes(tmp_path: Path) -> None:
    store = _init_workspace(tmp_path)
    store.write_selected_text("slug", TextFormat.markdown, "# Slug\n\nlocked\n")
    seed_candidate = Candidate(asset_id="slug", content="# Slug\n\nlocked\n")
    store.write_candidate(seed_candidate)

    creator = FakeCreatorRunner(
        layout=store.layout,
        replies=[{"done": True, "candidate": {"content": "# Slug\n\nchanged\n"}}],
    )
    reviewer = FakeReviewerRunner(
        layout=store.layout,
        reviewer="reviewer_a",
        replies=[{"verdict": "ok"}],
    )

    protocol_state = run_review_loop_orchestrator(
        workspace=store.layout.root,
        asset_id="slug",
        max_iterations=1,
        creator=creator,
        reviewer=reviewer,
    )

    assert protocol_state.decision is not None
    assert protocol_state.decision.outcome == LoopOutcome.needs_human

    review = store.read_review("slug", 1, reviewer="reviewer_a")
    assert review.verdict == ReviewVerdict.changes_requested
    assert any(issue.code == "locked_selection" for issue in review.issues)
