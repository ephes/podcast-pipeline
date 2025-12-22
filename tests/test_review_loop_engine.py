from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from podcast_pipeline.domain.models import (
    Candidate,
    ReviewIteration,
    ReviewVerdict,
    TextFormat,
)
from podcast_pipeline.review_loop_engine import (
    CreatorInput,
    CreatorOutput,
    LoopDecision,
    LoopOutcome,
    LoopProtocolState,
    ReviewerInput,
    run_review_loop_engine,
)
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


def _fixed_dt() -> datetime:
    return datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)


def _candidate(asset_id: str, iteration: int) -> Candidate:
    return Candidate(
        candidate_id=UUID(f"01234567-89ab-cdef-0123-456789abcde{iteration}"),
        asset_id=asset_id,
        format=TextFormat.markdown,
        content=f"draft {iteration}",
        created_at=_fixed_dt(),
    )


def test_engine_converges_when_reviewer_ok_and_creator_done(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)

    def creator(inp: CreatorInput) -> CreatorOutput:
        return CreatorOutput(candidate=_candidate("description", inp.iteration), done=inp.iteration == 2)

    def reviewer(inp: ReviewerInput) -> ReviewIteration:
        verdict = ReviewVerdict.changes_requested if inp.iteration == 1 else ReviewVerdict.ok
        return ReviewIteration(iteration=inp.iteration, verdict=verdict, reviewer="reviewer_a", created_at=_fixed_dt())

    state, writes = run_review_loop_engine(
        layout=layout,
        asset_id="description",
        max_iterations=5,
        creator=creator,
        reviewer=reviewer,
    )

    assert state.decision is not None
    assert state.decision.outcome == LoopOutcome.converged
    assert state.decision.final_iteration == 2
    assert len(state.iterations) == 2

    assert writes[0].path == tmp_path / "copy" / "protocol" / "description" / "iteration_01.json"
    assert writes[1].path == tmp_path / "copy" / "protocol" / "description" / "iteration_02.json"
    assert writes[-1].path == tmp_path / "copy" / "protocol" / "description" / "state.json"


def test_engine_stops_on_iteration_limit_with_needs_human(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)

    def creator(inp: CreatorInput) -> CreatorOutput:
        return CreatorOutput(candidate=_candidate("description", inp.iteration), done=False)

    def reviewer(inp: ReviewerInput) -> ReviewIteration:
        return ReviewIteration(
            iteration=inp.iteration,
            verdict=ReviewVerdict.changes_requested,
            reviewer="reviewer_a",
            created_at=_fixed_dt(),
        )

    state, writes = run_review_loop_engine(
        layout=layout,
        asset_id="description",
        max_iterations=2,
        creator=creator,
        reviewer=reviewer,
    )

    assert state.decision is not None
    assert state.decision.outcome == LoopOutcome.needs_human
    assert state.decision.final_iteration == 2
    assert state.decision.reason == "iteration_limit"

    assert len(state.iterations) == 2
    assert len(writes) == 3


def test_engine_does_not_stop_on_reviewer_needs_human(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)

    def creator(inp: CreatorInput) -> CreatorOutput:
        return CreatorOutput(candidate=_candidate("description", inp.iteration), done=False)

    def reviewer(inp: ReviewerInput) -> ReviewIteration:
        return ReviewIteration(
            iteration=inp.iteration,
            verdict=ReviewVerdict.needs_human,
            reviewer="reviewer_a",
            created_at=_fixed_dt(),
        )

    state, _ = run_review_loop_engine(
        layout=layout,
        asset_id="description",
        max_iterations=3,
        creator=creator,
        reviewer=reviewer,
    )

    assert state.decision is not None
    assert state.decision.outcome == LoopOutcome.needs_human
    assert state.decision.final_iteration == 3
    assert state.decision.reason == "iteration_limit"
    assert len(state.iterations) == 3
    assert all(it.review.verdict == ReviewVerdict.needs_human for it in state.iterations)


def test_engine_ignores_creator_done_when_reviewer_needs_human(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)

    def creator(inp: CreatorInput) -> CreatorOutput:
        return CreatorOutput(candidate=_candidate("description", inp.iteration), done=True)

    def reviewer(inp: ReviewerInput) -> ReviewIteration:
        return ReviewIteration(
            iteration=inp.iteration,
            verdict=ReviewVerdict.needs_human,
            reviewer="reviewer_a",
            created_at=_fixed_dt(),
        )

    state, _ = run_review_loop_engine(
        layout=layout,
        asset_id="description",
        max_iterations=2,
        creator=creator,
        reviewer=reviewer,
    )

    assert state.decision is not None
    assert state.decision.outcome == LoopOutcome.needs_human
    assert state.decision.final_iteration == 2
    assert state.decision.reason == "iteration_limit"
    assert len(state.iterations) == 2


def test_engine_respects_locked_outcome_and_does_not_rerun(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    existing = LoopProtocolState(
        asset_id="description",
        max_iterations=3,
        iterations=(),
        decision=LoopDecision(
            outcome=LoopOutcome.needs_human,
            final_iteration=1,
            reason="iteration_limit",
            locked_fields=frozenset({"outcome"}),
        ),
    )

    def creator(_: CreatorInput) -> CreatorOutput:
        raise AssertionError("creator should not be called when outcome is locked")

    def reviewer(_: ReviewerInput) -> ReviewIteration:
        raise AssertionError("reviewer should not be called when outcome is locked")

    state, writes = run_review_loop_engine(
        layout=layout,
        asset_id="description",
        max_iterations=3,
        creator=creator,
        reviewer=reviewer,
        existing=existing,
    )

    assert state.decision is not None
    assert state.decision.outcome == LoopOutcome.needs_human
    assert writes == ()
