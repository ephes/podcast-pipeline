from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest

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
    LoopProtocolIteration,
    LoopProtocolState,
    ReviewerInput,
    load_loop_protocol_state,
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


@pytest.mark.parametrize("resume", [False, True], ids=["fresh", "resume"])
def test_engine_refuses_boolean_limit_before_callbacks(tmp_path: Path, resume: bool) -> None:
    layout = EpisodeWorkspaceLayout(tmp_path)
    state, writes = run_review_loop_engine(
        layout=layout,
        asset_id="description",
        max_iterations=1,
        creator=lambda inp: CreatorOutput(candidate=_candidate(inp.asset_id, inp.iteration), done=True),
        reviewer=lambda inp: ReviewIteration(iteration=inp.iteration, verdict=ReviewVerdict.ok),
    )
    write = writes[-1]
    write.path.parent.mkdir(parents=True)
    write.path.write_text(write.dumps(), encoding="utf-8")
    saved = load_loop_protocol_state(write.path)
    assert saved == state

    creator = Mock(side_effect=AssertionError("unexpected creator call"))
    reviewer = Mock(side_effect=AssertionError("unexpected reviewer call"))
    checkpoint = Mock(side_effect=AssertionError("unexpected checkpoint call"))
    with pytest.raises(ValueError, match="max_iterations must be a positive integer"):
        run_review_loop_engine(
            layout=layout,
            asset_id="description",
            max_iterations=True,
            creator=creator,
            reviewer=reviewer,
            existing=saved if resume else None,
            checkpoint=checkpoint,
        )
    creator.assert_not_called()
    reviewer.assert_not_called()
    checkpoint.assert_not_called()


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


def test_engine_stops_immediately_on_reviewer_needs_human(tmp_path: Path) -> None:
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
    assert state.decision.final_iteration == 1
    assert state.decision.reason == "reviewer_requested_human"
    assert len(state.iterations) == 1
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
    assert state.decision.final_iteration == 1
    assert state.decision.reason == "reviewer_requested_human"
    assert len(state.iterations) == 1


def test_engine_respects_locked_outcome_and_does_not_rerun(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    existing = LoopProtocolState(
        asset_id="description",
        max_iterations=3,
        iterations=(
            LoopProtocolIteration(
                1,
                False,
                _candidate("description", 1),
                ReviewIteration(iteration=1, verdict=ReviewVerdict.needs_human),
            ),
        ),
        decision=LoopDecision(
            outcome=LoopOutcome.needs_human,
            final_iteration=1,
            reason="reviewer_requested_human",
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


def test_changed_candidate_identity_is_rejected_before_second_review(tmp_path: Path) -> None:
    import pytest

    original = _candidate("description", 1)
    reviews: list[int] = []

    def creator(inp: CreatorInput) -> CreatorOutput:
        return CreatorOutput(candidate=original.model_copy(update={"content": f"draft {inp.iteration}"}), done=False)

    def reviewer(inp: ReviewerInput) -> ReviewIteration:
        reviews.append(inp.iteration)
        return ReviewIteration(iteration=inp.iteration, verdict=ReviewVerdict.changes_requested)

    with pytest.raises(ValueError, match="candidate identity"):
        run_review_loop_engine(
            layout=EpisodeWorkspaceLayout(tmp_path),
            asset_id="description",
            max_iterations=3,
            creator=creator,
            reviewer=reviewer,
        )
    assert reviews == [1]


def test_identical_candidate_identity_can_be_reused(tmp_path: Path) -> None:
    original = _candidate("description", 1)
    state, _ = run_review_loop_engine(
        layout=EpisodeWorkspaceLayout(tmp_path),
        asset_id="description",
        max_iterations=2,
        creator=lambda inp: CreatorOutput(candidate=original, done=False),
        reviewer=lambda inp: ReviewIteration(iteration=inp.iteration, verdict=ReviewVerdict.changes_requested),
    )
    assert [it.candidate for it in state.iterations] == [original, original]


def test_callbacks_cannot_mutate_completed_evidence(tmp_path: Path) -> None:
    original = _candidate("description", 1)

    def creator(inp: CreatorInput) -> CreatorOutput:
        if inp.previous_candidate is not None:
            inp.previous_candidate.content = "mutated previous candidate"
        return CreatorOutput(candidate=_candidate("description", inp.iteration), done=False)

    def reviewer(inp: ReviewerInput) -> ReviewIteration:
        inp.candidate.content = "mutated reviewer input"
        return ReviewIteration(iteration=inp.iteration, verdict=ReviewVerdict.changes_requested)

    state, _ = run_review_loop_engine(
        layout=EpisodeWorkspaceLayout(tmp_path),
        asset_id="description",
        max_iterations=2,
        creator=creator,
        reviewer=reviewer,
    )
    assert state.iterations[0].candidate == original


def test_direct_existing_state_is_preflighted_before_callbacks(tmp_path: Path) -> None:
    import pytest

    existing = LoopProtocolState(
        asset_id="description",
        max_iterations=3,
        iterations=(
            LoopProtocolIteration(
                2,
                False,
                _candidate("description", 1),
                ReviewIteration(iteration=2, verdict=ReviewVerdict.changes_requested),
            ),
        ),
    )

    def creator(inp: CreatorInput) -> CreatorOutput:
        raise AssertionError("invalid state must fail before creator")

    def reviewer(inp: ReviewerInput) -> ReviewIteration:
        raise AssertionError("invalid state must fail before reviewer")

    with pytest.raises(ValueError, match="protocol state"):
        run_review_loop_engine(
            layout=EpisodeWorkspaceLayout(tmp_path),
            asset_id="description",
            max_iterations=3,
            creator=creator,
            reviewer=reviewer,
            existing=existing,
        )
