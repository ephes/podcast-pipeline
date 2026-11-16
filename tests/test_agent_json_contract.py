from __future__ import annotations

import json

import pytest

from podcast_pipeline.agent_runners import AgentRunnerError, extract_json_payload


@pytest.mark.parametrize(
    "raw",
    [
        '{"done":false,"done":true}',
        '{"review":{"verdict":"ok","verdict":"changes_requested"}}',
        '{"candidate":{"content":"first","content":"second"}}',
        'sample {"done":false} actual {"done":true}',
        'prefix {"done":true} suffix',
        "```json\n{}\n```\n{}",
        "[]",
        '{"n":NaN}',
        '{"n":1e999}',
        '{"s":"\\ud800"}',
    ],
)
def test_ambiguous_or_invalid_results_refused(raw: str) -> None:
    with pytest.raises(AgentRunnerError, match="JSON"):
        extract_json_payload(raw, label="Synthetic")


@pytest.mark.parametrize(
    "raw",
    [
        '{"value":' + "[" * 65 + "0" + "]" * 65 + "}",
        '{"n":' + "9" * 5000 + "}",
        '{"values":[' + ",".join("0" for _ in range(100_001)) + "]}",
    ],
)
def test_structural_work_refused(raw: str) -> None:
    with pytest.raises(AgentRunnerError, match="JSON"):
        extract_json_payload(raw, label="Synthetic")


def test_supported_fence_and_exact_values() -> None:
    expected = {"content": "Grüße {value}", "done": True, "score": 0.125}
    assert extract_json_payload("```json\n" + json.dumps(expected) + "\n```", label="Synthetic") == expected
    assert extract_json_payload(json.dumps(expected), label="Synthetic") == expected


def test_excess_work_refused_before_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    from podcast_pipeline import control_json

    monkeypatch.setattr(control_json, "MAX_JSON_TOKENS", 4)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("decoder must not hydrate excessive input")

    monkeypatch.setattr(control_json.json, "loads", forbidden)
    with pytest.raises(AgentRunnerError):
        extract_json_payload('{"values":[0,0,0]}', label="Synthetic")


@pytest.mark.parametrize(
    "raw",
    [
        '"' + '\\"' * 200_000,
        '{"a":"' + "\\" * 400_001,
        '{"a":"x' + '"\\' * 200_000,
    ],
)
def test_unterminated_string_admission_is_linear(raw: str) -> None:
    import time

    started = time.monotonic()
    with pytest.raises(AgentRunnerError, match="JSON"):
        extract_json_payload(raw, label="Synthetic")
    assert time.monotonic() - started < 2.0


def test_escaped_quotes_inside_strings_still_admitted() -> None:
    expected = {"text": 'say "hi" \\ done', "list": ["\\", '"']}
    assert extract_json_payload(json.dumps(expected), label="Synthetic") == expected
