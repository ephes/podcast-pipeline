from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
