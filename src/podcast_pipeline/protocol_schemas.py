from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from podcast_pipeline.domain.intermediate_formats import ChunkSummary, EpisodeSummary
from podcast_pipeline.domain.models import Candidate, ReviewIteration


def candidate_json_schema() -> dict[str, Any]:
    """JSON schema for copy/candidates/... artifacts."""
    return Candidate.model_json_schema()


def review_iteration_json_schema() -> dict[str, Any]:
    """JSON schema for copy/reviews/... iteration artifacts."""
    return ReviewIteration.model_json_schema()


def validate_candidate_payload(payload: Mapping[str, Any]) -> Candidate:
    return Candidate.model_validate(payload)


def validate_review_iteration_payload(payload: Mapping[str, Any]) -> ReviewIteration:
    return ReviewIteration.model_validate(payload)


def parse_candidate_json(raw: str | bytes | bytearray) -> Candidate:
    return Candidate.model_validate_json(raw)


def parse_review_iteration_json(raw: str | bytes | bytearray) -> ReviewIteration:
    return ReviewIteration.model_validate_json(raw)


def chunk_summary_json_schema() -> dict[str, Any]:
    """JSON schema for chunk summary artifacts."""
    return ChunkSummary.model_json_schema()


def episode_summary_json_schema() -> dict[str, Any]:
    """JSON schema for episode summary artifacts."""
    return EpisodeSummary.model_json_schema()


def asset_candidates_response_json_schema(
    *,
    num_candidates: int | None = None,
) -> dict[str, Any]:
    """JSON schema for the top-level asset candidates LLM response.

    Wraps the Candidate schema in a ``{"candidates": [...]}`` envelope.
    When *num_candidates* is given, ``minItems`` and ``maxItems`` are set
    so the schema encodes the exact count constraint.
    """
    candidate_schema = Candidate.model_json_schema()
    candidates_prop: dict[str, Any] = {
        "type": "array",
        "items": candidate_schema,
    }
    if num_candidates is not None:
        if num_candidates < 1:
            raise ValueError(f"num_candidates must be >= 1, got {num_candidates}")
        candidates_prop["minItems"] = num_candidates
        candidates_prop["maxItems"] = num_candidates
    return {
        "type": "object",
        "properties": {
            "candidates": candidates_prop,
        },
        "required": ["candidates"],
        "additionalProperties": False,
    }
