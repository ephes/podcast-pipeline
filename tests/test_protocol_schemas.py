from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from podcast_pipeline.agent_runners import AgentRunnerError, extract_json_payload
from podcast_pipeline.domain.models import DomainModel, IssueSeverity, ReviewVerdict, TextFormat
from podcast_pipeline.protocol_schemas import (
    asset_candidates_response_json_schema,
    candidate_json_schema,
    parse_candidate_json,
    parse_review_iteration_json,
    review_iteration_json_schema,
    validate_candidate_payload,
    validate_review_iteration_payload,
)


def test_candidate_json_parsing_defaults_format() -> None:
    candidate = parse_candidate_json('{"asset_id": "description", "content": "draft"}')
    assert candidate.asset_id == "description"
    assert candidate.content == "draft"
    assert candidate.format == TextFormat.markdown


@pytest.mark.parametrize("representation", [str, bytes, bytearray])
@pytest.mark.parametrize(
    ("parse_artifact", "validate_payload", "raw", "ambiguous"),
    [
        (
            parse_candidate_json,
            validate_candidate_payload,
            '{"asset_id":"description","content":"Entwurf für Testfolge",'
            '"provenance":[{"kind":"prompt","ref":"synthetic"}]}',
            '{"asset_id":"description","content":"Entwurf für Testfolge",'
            '"provenance":[{"kind":"draft","kind":"prompt","ref":"synthetic"}]}',
        ),
        (
            parse_review_iteration_json,
            validate_review_iteration_payload,
            '{"iteration":1,"verdict":"changes_requested","issues":[{"message":"Kürzen"}]}',
            '{"iteration":1,"verdict":"changes_requested","issues":[{"message":"Überarbeiten","message":"Kürzen"}]}',
        ),
    ],
)
def test_artifact_parsers_share_pipeline_json_admission(
    parse_artifact: Callable[[str | bytes | bytearray], DomainModel],
    validate_payload: Callable[[dict[str, Any]], DomainModel],
    raw: str,
    ambiguous: str,
    representation: type[str] | type[bytes] | type[bytearray],
) -> None:
    admitted = extract_json_payload(raw, label="Synthetic")
    actual = parse_artifact(raw if representation is str else representation(raw.encode("utf-8")))
    assert actual.model_dump(exclude_unset=True) == validate_payload(admitted).model_dump(exclude_unset=True)

    with pytest.raises(AgentRunnerError, match="invalid or excessive JSON"):
        extract_json_payload(ambiguous, label="Synthetic")
    with pytest.raises(ValueError, match="Invalid or excessive control JSON"):
        parse_artifact(ambiguous if representation is str else representation(ambiguous.encode("utf-8")))


def test_review_iteration_json_rejects_invalid_verdict() -> None:
    with pytest.raises(ValidationError):
        parse_review_iteration_json('{"iteration": 1, "verdict": "nope"}')


def test_review_iteration_json_rejects_invalid_issue_severity() -> None:
    raw = json.dumps(
        {
            "iteration": 1,
            "verdict": "changes_requested",
            "issues": [{"message": "bad", "severity": "nope"}],
        },
    )
    with pytest.raises(ValidationError):
        parse_review_iteration_json(raw)


def test_review_iteration_json_rejects_error_issue_with_ok_verdict() -> None:
    raw = json.dumps(
        {
            "iteration": 1,
            "verdict": "ok",
            "issues": [{"message": "blocking", "severity": "error"}],
        },
    )
    with pytest.raises(ValidationError):
        parse_review_iteration_json(raw)


def _find_schema_enum(schema: Any, title: str) -> list[str]:
    stack = [schema]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if current.get("title") == title and "enum" in current:
                enum_values = current["enum"]
                if not isinstance(enum_values, list):
                    raise AssertionError(f"schema enum for {title} is not a list")
                return [str(value) for value in enum_values]
            for value in current.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            for value in current:
                if isinstance(value, (dict, list)):
                    stack.append(value)
    raise AssertionError(f"schema enum {title} not found")


def test_candidate_schema_exposes_format_enum() -> None:
    schema = candidate_json_schema()
    assert set(_find_schema_enum(schema, "TextFormat")) == {member.value for member in TextFormat}


def test_review_schema_exposes_verdict_enum() -> None:
    schema = review_iteration_json_schema()
    assert set(_find_schema_enum(schema, "ReviewVerdict")) == {member.value for member in ReviewVerdict}


def test_review_schema_exposes_issue_severity_enum() -> None:
    schema = review_iteration_json_schema()
    assert set(_find_schema_enum(schema, "IssueSeverity")) == {member.value for member in IssueSeverity}


def test_asset_candidates_response_schema_without_count() -> None:
    schema = asset_candidates_response_json_schema()
    assert schema["type"] == "object"
    assert schema["required"] == ["candidates"]
    assert schema["additionalProperties"] is False

    candidates_prop = schema["properties"]["candidates"]
    assert candidates_prop["type"] == "array"
    assert "items" in candidates_prop
    assert "minItems" not in candidates_prop
    assert "maxItems" not in candidates_prop


def test_asset_candidates_response_schema_with_count() -> None:
    schema = asset_candidates_response_json_schema(num_candidates=5)

    candidates_prop = schema["properties"]["candidates"]
    assert candidates_prop["type"] == "array"
    assert candidates_prop["minItems"] == 5
    assert candidates_prop["maxItems"] == 5


@pytest.mark.parametrize("bad_value", [0, -1, -10])
def test_asset_candidates_response_schema_rejects_invalid_count(bad_value: int) -> None:
    with pytest.raises(ValueError, match="num_candidates must be >= 1"):
        asset_candidates_response_json_schema(num_candidates=bad_value)
