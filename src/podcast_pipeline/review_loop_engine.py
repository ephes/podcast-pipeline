from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from podcast_pipeline.domain.models import Candidate, ReviewIteration, ReviewVerdict
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


class LoopOutcome(StrEnum):
    converged = "converged"
    needs_human = "needs_human"
    in_progress = "in_progress"


@dataclass(frozen=True)
class LoopDecision:
    outcome: LoopOutcome
    final_iteration: int | None = None
    reason: str | None = None
    locked_fields: frozenset[str] = frozenset()

    def is_terminal(self) -> bool:
        return self.outcome in (LoopOutcome.converged, LoopOutcome.needs_human)

    def is_locked(self, field: str) -> bool:
        return field in self.locked_fields


@dataclass(frozen=True)
class LoopProtocolIteration:
    iteration: int
    creator_done: bool
    candidate: Candidate
    review: ReviewIteration

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "iteration": self.iteration,
            "creator": {
                "done": self.creator_done,
                "candidate": self.candidate.model_dump(mode="json"),
            },
            "reviewer": self.review.model_dump(mode="json"),
        }


@dataclass(frozen=True)
class LoopProtocolState:
    asset_id: str
    max_iterations: int
    iterations: tuple[LoopProtocolIteration, ...] = ()
    decision: LoopDecision | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "asset_id": self.asset_id,
            "max_iterations": self.max_iterations,
            "decision": None if self.decision is None else _decision_to_json(self.decision),
            "iterations": [it.to_json_dict() for it in self.iterations],
        }


@dataclass(frozen=True)
class ProtocolWrite:
    path: Path
    json_data: dict[str, Any]

    def dumps(self) -> str:
        return json.dumps(self.json_data, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class CreatorInput:
    asset_id: str
    iteration: int
    previous_candidate: Candidate | None
    previous_review: ReviewIteration | None


@dataclass(frozen=True)
class CreatorOutput:
    candidate: Candidate
    done: bool


@dataclass(frozen=True)
class ReviewerInput:
    asset_id: str
    iteration: int
    candidate: Candidate


def run_review_loop_engine(
    *,
    layout: EpisodeWorkspaceLayout,
    asset_id: str,
    max_iterations: int,
    creator: Callable[[CreatorInput], CreatorOutput],
    reviewer: Callable[[ReviewerInput], ReviewIteration],
    existing: LoopProtocolState | None = None,
) -> tuple[LoopProtocolState, tuple[ProtocolWrite, ...]]:
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")

    state = existing or LoopProtocolState(asset_id=asset_id, max_iterations=max_iterations)
    if state.asset_id != asset_id:
        raise ValueError("existing.asset_id must match asset_id")
    if state.max_iterations != max_iterations:
        raise ValueError("existing.max_iterations must match max_iterations")

    if state.decision is not None and state.decision.is_terminal() and state.decision.is_locked("outcome"):
        return state, ()

    writes: list[ProtocolWrite] = []
    iterations: list[LoopProtocolIteration] = list(state.iterations)

    prev_candidate: Candidate | None = iterations[-1].candidate if iterations else None
    prev_review: ReviewIteration | None = iterations[-1].review if iterations else None
    start_iteration = (iterations[-1].iteration + 1) if iterations else 1

    decision: LoopDecision | None = state.decision

    for iteration in range(start_iteration, max_iterations + 1):
        creator_out = creator(
            CreatorInput(
                asset_id=asset_id,
                iteration=iteration,
                previous_candidate=prev_candidate,
                previous_review=prev_review,
            ),
        )
        if creator_out.candidate.asset_id != asset_id:
            raise ValueError("creator output candidate.asset_id must match asset_id")

        review_out = reviewer(
            ReviewerInput(
                asset_id=asset_id,
                iteration=iteration,
                candidate=creator_out.candidate,
            ),
        )
        review_out = _normalize_review_iteration(review_out, iteration)

        protocol_iteration = LoopProtocolIteration(
            iteration=iteration,
            creator_done=creator_out.done,
            candidate=creator_out.candidate,
            review=review_out,
        )
        iterations.append(protocol_iteration)
        writes.append(
            ProtocolWrite(
                path=layout.protocol_iteration_json_path(asset_id, iteration),
                json_data=protocol_iteration.to_json_dict(),
            ),
        )

        prev_candidate = creator_out.candidate
        prev_review = review_out

        decision = _decide_outcome(
            review=review_out,
            creator_done=creator_out.done,
            iteration=iteration,
            max_iterations=max_iterations,
        )
        if decision is not None:
            break

    state2 = LoopProtocolState(
        asset_id=asset_id,
        max_iterations=max_iterations,
        iterations=tuple(iterations),
        decision=_merge_decision(state.decision, decision),
    )
    writes.append(ProtocolWrite(path=layout.protocol_state_json_path(asset_id), json_data=state2.to_json_dict()))
    return state2, tuple(writes)


def _normalize_review_iteration(review: ReviewIteration, iteration: int) -> ReviewIteration:
    if review.iteration == iteration:
        return review
    return review.model_copy(update={"iteration": iteration})


def _terminal_decision(*, outcome: LoopOutcome, iteration: int, reason: str) -> LoopDecision:
    return LoopDecision(
        outcome=outcome,
        final_iteration=iteration,
        reason=reason,
        locked_fields=frozenset({"outcome", "final_iteration", "reason"}),
    )


def _decide_outcome(
    *,
    review: ReviewIteration,
    creator_done: bool,
    iteration: int,
    max_iterations: int,
) -> LoopDecision | None:
    if review.verdict == ReviewVerdict.ok and creator_done:
        return _terminal_decision(
            outcome=LoopOutcome.converged,
            iteration=iteration,
            reason="reviewer_ok_and_creator_done",
        )
    if iteration >= max_iterations:
        return _terminal_decision(outcome=LoopOutcome.needs_human, iteration=iteration, reason="iteration_limit")
    return None


def _decision_to_json(decision: LoopDecision) -> dict[str, Any]:
    return {
        "outcome": decision.outcome.value,
        "final_iteration": decision.final_iteration,
        "reason": decision.reason,
        "locked_fields": sorted(decision.locked_fields),
    }


def _merge_decision(existing: LoopDecision | None, proposed: LoopDecision | None) -> LoopDecision | None:
    if proposed is None:
        return existing
    if existing is None:
        return proposed

    locked = existing.locked_fields
    outcome = existing.outcome if "outcome" in locked else proposed.outcome
    final_iteration = existing.final_iteration if "final_iteration" in locked else proposed.final_iteration
    reason = existing.reason if "reason" in locked else proposed.reason
    return LoopDecision(
        outcome=outcome,
        final_iteration=final_iteration,
        reason=reason,
        locked_fields=locked | proposed.locked_fields,
    )
