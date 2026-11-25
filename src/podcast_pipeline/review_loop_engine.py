from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, field_validator
from pydantic_core import to_jsonable_python

from podcast_pipeline.domain.models import Candidate, ReviewIteration, ReviewVerdict
from podcast_pipeline.file_inputs import read_regular_bytes
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
                # Keep nonfinite floats visible to the strict writer. Pydantic's
                # JSON-mode model dump would turn them into null.
                "candidate": to_jsonable_python(self.candidate.model_dump(), inf_nan_mode="constants"),
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


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _DecisionDocument(_Document):
    outcome: LoopOutcome
    final_iteration: StrictInt | None
    reason: str | None
    locked_fields: frozenset[str]


class _CreatorDocument(_Document):
    done: StrictBool
    candidate: Candidate

    @field_validator("candidate", mode="before")
    @classmethod
    def complete_candidate(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not Candidate.model_fields.keys() <= value.keys():
            raise ValueError("incomplete persisted candidate")
        return value


class _IterationDocument(_Document):
    version: Literal[1]
    iteration: StrictInt
    creator: _CreatorDocument
    reviewer: ReviewIteration

    @field_validator("reviewer", mode="before")
    @classmethod
    def complete_review(cls, value: Any) -> Any:
        if (
            not isinstance(value, dict)
            or not ReviewIteration.model_fields.keys() <= value.keys()
            or type(value.get("iteration")) is not int
        ):
            raise ValueError("incomplete or invalid persisted review")
        return value


class _StateDocument(_Document):
    version: Literal[1]
    asset_id: str
    max_iterations: StrictInt
    iterations: list[_IterationDocument]
    decision: _DecisionDocument | None


class LoopProtocolError(ValueError):
    """Invalid review evidence; diagnostics never include candidate content."""


def parse_loop_protocol_state(raw: Any) -> LoopProtocolState:
    """Decode and validate the versioned resume envelope before callbacks or writes."""
    try:
        document = _StateDocument.model_validate(raw)
        state = LoopProtocolState(
            asset_id=document.asset_id,
            max_iterations=document.max_iterations,
            iterations=tuple(
                LoopProtocolIteration(it.iteration, it.creator.done, it.creator.candidate, it.reviewer)
                for it in document.iterations
            ),
            decision=None if document.decision is None else LoopDecision(**document.decision.model_dump()),
        )
        _validate_state_relations(state)
        return state
    except (ValueError, TypeError, RecursionError):
        raise LoopProtocolError("Invalid review protocol state; restore or repair the saved evidence.") from None


def _validate_state_relations(state: LoopProtocolState) -> None:
    if state.max_iterations < 1 or len(state.iterations) > state.max_iterations:
        raise ValueError("invalid iteration limit")
    candidates: dict[object, Candidate] = {}
    expected = None
    for number, it in enumerate(state.iterations, start=1):
        if it.iteration != number or it.review.iteration != number or it.candidate.asset_id != state.asset_id:
            raise ValueError("invalid iteration identity")
        _check_candidate_identity(it.candidate, candidates)
        candidates[it.candidate.candidate_id] = it.candidate
        expected = _decide_outcome(
            review=it.review, creator_done=it.creator_done, iteration=number, max_iterations=state.max_iterations
        )
        if expected is not None and number != len(state.iterations):
            raise ValueError("iteration after terminal review")
    _validate_terminal_decision(state, expected)


def _validate_terminal_decision(state: LoopProtocolState, expected: LoopDecision | None) -> None:
    decision = state.decision
    if decision is None:
        if state.iterations and expected is not None:
            raise ValueError("missing terminal decision")
        return
    if (
        not decision.is_terminal()
        or not decision.is_locked("outcome")
        or decision.final_iteration != len(state.iterations)
        or not state.iterations
        or not decision.locked_fields <= {"outcome", "final_iteration", "reason"}
    ):
        raise ValueError("invalid terminal decision")
    if decision.reason == "selection_conflict":
        if (
            decision.outcome != LoopOutcome.needs_human
            or expected is None
            or expected.outcome != LoopOutcome.converged
        ):
            raise ValueError("invalid selection conflict")
    elif expected is None or (decision.outcome, decision.reason) != (expected.outcome, expected.reason):
        raise ValueError("inconsistent terminal decision")


def _check_candidate_identity(candidate: Candidate, known: dict[object, Candidate]) -> None:
    previous = known.get(candidate.candidate_id)
    if previous is not None and previous != candidate:
        raise LoopProtocolError("Review candidate identity cannot change; use a fresh candidate UUID.")


def load_loop_protocol_state(path: Path) -> LoopProtocolState:
    try:
        body = read_regular_bytes(path, max_bytes=8 * 1024 * 1024)
        return parse_loop_protocol_state(
            json.loads(
                body,
                object_pairs_hook=unique_json_members,
                parse_constant=_reject_nonfinite_constant,
                parse_float=_parse_finite_float,
            )
        )
    except (OSError, ValueError, RecursionError):
        raise LoopProtocolError("Invalid review protocol state; restore or repair the saved evidence.") from None


def _reject_nonfinite_constant(_: str) -> float:
    raise ValueError("nonfinite JSON number")


def _parse_finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def unique_json_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON object hook that refuses duplicate members instead of keeping the last one."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


@dataclass(frozen=True)
class ProtocolWrite:
    path: Path
    json_data: dict[str, Any]

    def dumps(self) -> str:
        try:
            return json.dumps(self.json_data, indent=2, sort_keys=True, allow_nan=False) + "\n"
        except ValueError:
            raise LoopProtocolError(
                "Cannot checkpoint review protocol state; check for nonfinite numbers or invalid JSON values."
            ) from None


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
    checkpoint: Callable[[LoopProtocolState, tuple[ProtocolWrite, ...]], None] | None = None,
) -> tuple[LoopProtocolState, tuple[ProtocolWrite, ...]]:
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or max_iterations < 1:
        raise ValueError("max_iterations must be a positive integer")

    state = (
        parse_loop_protocol_state(existing.to_json_dict())
        if existing is not None
        else LoopProtocolState(asset_id=asset_id, max_iterations=max_iterations)
    )
    if state.asset_id != asset_id:
        raise LoopProtocolError("Review protocol state asset_id must match asset_id")
    if state.max_iterations != max_iterations:
        raise LoopProtocolError("Review protocol state max_iterations must match max_iterations")

    if state.decision is not None and state.decision.is_terminal() and state.decision.is_locked("outcome"):
        return state, ()

    writes: list[ProtocolWrite] = []
    iterations: list[LoopProtocolIteration] = list(state.iterations)

    prev_candidate: Candidate | None = iterations[-1].candidate if iterations else None
    prev_review: ReviewIteration | None = iterations[-1].review if iterations else None
    start_iteration = (iterations[-1].iteration + 1) if iterations else 1

    decision: LoopDecision | None = state.decision
    known: dict[object, Candidate] = {it.candidate.candidate_id: it.candidate for it in iterations}

    for iteration in range(start_iteration, max_iterations + 1):
        creator_out = creator(
            CreatorInput(
                asset_id=asset_id,
                iteration=iteration,
                previous_candidate=None if prev_candidate is None else prev_candidate.model_copy(deep=True),
                previous_review=None if prev_review is None else prev_review.model_copy(deep=True),
            ),
        )
        if creator_out.candidate.asset_id != asset_id:
            raise ValueError("creator output candidate.asset_id must match asset_id")

        candidate = Candidate.model_validate(creator_out.candidate.model_dump()).model_copy(deep=True)
        _check_candidate_identity(candidate, known)
        known[candidate.candidate_id] = candidate
        review_out = reviewer(
            ReviewerInput(
                asset_id=asset_id,
                iteration=iteration,
                candidate=candidate.model_copy(deep=True),
            ),
        )
        review_out = _normalize_review_iteration(review_out, iteration).model_copy(deep=True)

        protocol_iteration = LoopProtocolIteration(
            iteration=iteration,
            creator_done=creator_out.done,
            candidate=candidate,
            review=review_out,
        )
        iterations.append(protocol_iteration)
        writes.append(
            ProtocolWrite(
                path=layout.protocol_iteration_json_path(asset_id, iteration),
                json_data=protocol_iteration.to_json_dict(),
            ),
        )

        prev_candidate = candidate
        prev_review = review_out

        decision = _decide_outcome(
            review=review_out,
            creator_done=creator_out.done,
            iteration=iteration,
            max_iterations=max_iterations,
        )
        completed = LoopProtocolState(
            asset_id=asset_id,
            max_iterations=max_iterations,
            iterations=tuple(iterations),
            decision=_merge_decision(state.decision, decision),
        )
        if checkpoint is not None:
            checkpoint(
                completed,
                (writes[-1], ProtocolWrite(layout.protocol_state_json_path(asset_id), completed.to_json_dict())),
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
    if review.verdict == ReviewVerdict.needs_human:
        return _terminal_decision(
            outcome=LoopOutcome.needs_human, iteration=iteration, reason="reviewer_requested_human"
        )
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
