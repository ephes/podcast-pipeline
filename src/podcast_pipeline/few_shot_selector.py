from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from podcast_pipeline.prompting import FewShotExample

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TAG_SPLIT_RE = re.compile(r"[;,]")


@dataclass(frozen=True)
class FewShotExampleRecord:
    example_id: str
    input_text: str
    output_text: str
    tags: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    title: str | None = None
    summary: str | None = None
    source: str | None = None

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | FewShotExampleRecord) -> FewShotExampleRecord:
        if isinstance(value, FewShotExampleRecord):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("Few-shot records must be mappings")
        input_text, output_text = _extract_io(value)
        example_id = _extract_example_id(value, input_text, output_text)
        return cls(
            example_id=example_id,
            input_text=input_text,
            output_text=output_text,
            tags=_coerce_str_list(value.get("tags")),
            topics=_coerce_str_list(value.get("topics")),
            keywords=_coerce_str_list(value.get("keywords")),
            title=_coerce_optional_str(value.get("title")),
            summary=_coerce_optional_str(value.get("summary")),
            source=_coerce_optional_str(value.get("source")),
        )

    def to_few_shot(self) -> FewShotExample:
        return FewShotExample(input_text=self.input_text, output_text=self.output_text)

    def match_score(self, topic_tokens: set[str]) -> int:
        if not topic_tokens:
            return 0
        tag_tokens = _tokens_from_values((*self.tags, *self.topics, *self.keywords))
        text_tokens = _tokens_from_values(
            [
                self.title,
                self.summary,
                self.input_text,
            ],
        )
        tag_hits = len(topic_tokens & tag_tokens)
        text_hits = len(topic_tokens & text_tokens)
        return (tag_hits * 3) + text_hits


ScoredRecord = tuple[int, int, FewShotExampleRecord]


def load_few_shot_records(path: Path) -> list[FewShotExampleRecord]:
    records: list[FewShotExampleRecord] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at line {line_number}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise TypeError(f"Few-shot JSONL records must be objects (line {line_number}).")
        records.append(FewShotExampleRecord.from_value(raw))
    return records


def select_few_shot_examples(
    *,
    examples: Sequence[FewShotExampleRecord | Mapping[str, Any]],
    topics: Iterable[str],
    limit: int = 5,
) -> tuple[FewShotExample, ...]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    records = [FewShotExampleRecord.from_value(example) for example in examples]
    if not records:
        return ()

    topic_tokens = _tokens_from_values(topics)
    if not topic_tokens:
        return tuple(record.to_few_shot() for record in records[:limit])

    scored = _score_records(records, topic_tokens)
    selected = _select_scored(scored, limit)
    if len(selected) < limit:
        selected = _fill_remaining(selected, scored, limit)

    return tuple(record.to_few_shot() for record in selected[:limit])


def _score_records(
    records: Sequence[FewShotExampleRecord],
    topic_tokens: set[str],
) -> list[ScoredRecord]:
    scored = [(record.match_score(topic_tokens), idx, record) for idx, record in enumerate(records)]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored


def _select_scored(scored: Sequence[ScoredRecord], limit: int) -> list[FewShotExampleRecord]:
    selected: list[FewShotExampleRecord] = []
    seen_ids: set[str] = set()
    for score, _, record in scored:
        if score <= 0:
            break
        if record.example_id in seen_ids:
            continue
        selected.append(record)
        seen_ids.add(record.example_id)
        if len(selected) >= limit:
            break
    return selected


def _fill_remaining(
    selected: list[FewShotExampleRecord],
    scored: Sequence[ScoredRecord],
    limit: int,
) -> list[FewShotExampleRecord]:
    seen_ids = {record.example_id for record in selected}
    for _, _, record in sorted(scored, key=lambda item: item[1]):
        if record.example_id in seen_ids:
            continue
        selected.append(record)
        seen_ids.add(record.example_id)
        if len(selected) >= limit:
            break
    return selected


def _extract_io(value: Mapping[str, Any]) -> tuple[str, str]:
    if "input" in value and "output" in value:
        input_text = value.get("input")
        output_text = value.get("output")
    else:
        input_text = value.get("user")
        output_text = value.get("assistant")
    if not isinstance(input_text, str) or not isinstance(output_text, str):
        raise TypeError("Few-shot records must include input/output strings")
    return input_text, output_text


def _extract_example_id(value: Mapping[str, Any], input_text: str, output_text: str) -> str:
    example_id = value.get("example_id") or value.get("id")
    if isinstance(example_id, str) and example_id.strip():
        return example_id
    digest = sha256(f"{input_text}\n{output_text}".encode()).hexdigest()[:12]
    return f"shot_{digest}"


def _coerce_optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value).strip() or None


def _coerce_str_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [item.strip() for item in _TAG_SPLIT_RE.split(value)]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = [str(item).strip() for item in value]
    else:
        return ()
    return tuple(item for item in items if item)


def _tokens_from_values(values: Iterable[object]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        if value is None:
            continue
        tokens.update(_TOKEN_RE.findall(str(value).lower()))
    return tokens
