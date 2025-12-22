from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from podcast_pipeline.domain.models import Candidate, ProvenanceRef, ReviewIteration
from podcast_pipeline.protocol_schemas import candidate_json_schema, review_iteration_json_schema
from podcast_pipeline.review_loop_engine import CreatorInput, ReviewerInput
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


@dataclass(frozen=True)
class GlossaryEntry:
    term: str
    definition: str

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | Sequence[str] | GlossaryEntry) -> GlossaryEntry:
        if isinstance(value, GlossaryEntry):
            return value
        if isinstance(value, Mapping):
            term = value.get("term")
            definition = value.get("definition")
            if not isinstance(term, str) or not isinstance(definition, str):
                raise TypeError("Glossary entries must include 'term' and 'definition' strings")
            return cls(term=term, definition=definition)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 2:
            term, definition = value
            if not isinstance(term, str) or not isinstance(definition, str):
                raise TypeError("Glossary tuple entries must be (term, definition) strings")
            return cls(term=term, definition=definition)
        raise TypeError("Glossary entries must be GlossaryEntry, mapping, or (term, definition) tuple")


@dataclass(frozen=True)
class FewShotExample:
    input_text: str
    output_text: str

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | FewShotExample) -> FewShotExample:
        if isinstance(value, FewShotExample):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("Few-shot examples must be mappings with input/output text")
        if "input" in value and "output" in value:
            input_text = value.get("input")
            output_text = value.get("output")
        else:
            input_text = value.get("user")
            output_text = value.get("assistant")
        if not isinstance(input_text, str) or not isinstance(output_text, str):
            raise TypeError("Few-shot examples must include input/output strings")
        return cls(input_text=input_text, output_text=output_text)


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    template: str
    description: str | None = None


@dataclass(frozen=True)
class PromptRenderResult:
    prompt_id: str
    template: str
    text: str
    context: dict[str, str]
    glossary: tuple[GlossaryEntry, ...]
    few_shots: tuple[FewShotExample, ...]

    def to_json_data(self) -> dict[str, Any]:
        return {
            "version": 1,
            "prompt_id": self.prompt_id,
            "template": self.template,
            "context": dict(self.context),
            "glossary": [{"term": entry.term, "definition": entry.definition} for entry in self.glossary],
            "few_shots": [{"input": example.input_text, "output": example.output_text} for example in self.few_shots],
            "prompt_text": self.text,
        }

    def provenance_ref(self) -> ProvenanceRef:
        return ProvenanceRef(kind="prompts", ref=self.prompt_id)


class PromptRegistry:
    def __init__(self, templates: Sequence[PromptTemplate]) -> None:
        self._templates = {template.name: template for template in templates}

    def get(self, name: str) -> PromptTemplate:
        if name not in self._templates:
            raise KeyError(f"Unknown prompt template: {name}")
        return self._templates[name]


class PromptRenderer:
    def __init__(self, registry: PromptRegistry) -> None:
        self._registry = registry

    def render(
        self,
        *,
        name: str,
        context: Mapping[str, str],
        glossary: Mapping[str, str] | Sequence[GlossaryEntry | Mapping[str, Any] | Sequence[str]] | None = None,
        few_shots: Sequence[FewShotExample | Mapping[str, Any]] | None = None,
    ) -> PromptRenderResult:
        template = self._registry.get(name)
        context_str = {key: str(value) for key, value in context.items()}
        base = template.template.format_map(context_str).rstrip()

        glossary_entries = _normalize_glossary(glossary)
        few_shot_entries = _normalize_few_shots(few_shots)

        sections = [base]
        if glossary_entries:
            sections.append(_render_glossary(glossary_entries))
        if few_shot_entries:
            sections.append(_render_few_shots(few_shot_entries))

        text = "\n\n".join(section for section in sections if section) + "\n"
        prompt_id = _prompt_ref(template.name, text)
        return PromptRenderResult(
            prompt_id=prompt_id,
            template=template.name,
            text=text,
            context=context_str,
            glossary=glossary_entries,
            few_shots=few_shot_entries,
        )


class PromptStore:
    def __init__(self, store: EpisodeWorkspaceStore) -> None:
        self._store = store

    def write(self, rendered: PromptRenderResult) -> ProvenanceRef:
        provenance = rendered.provenance_ref()
        self._store.write_provenance_json(provenance, rendered.to_json_data())
        return provenance


_CREATOR_TEMPLATE = """You are the Creator agent for podcast copy.

Asset id: {asset_id}
Iteration: {iteration}

Previous candidate (JSON):
{previous_candidate_json}

Previous review (JSON):
{previous_review_json}

Return JSON with fields:
- applied (bool)
- done (bool)
- candidate (Candidate schema below)

Candidate schema:
{candidate_schema}
"""


_REVIEWER_TEMPLATE = """You are the Reviewer agent for podcast copy.

Asset id: {asset_id}
Iteration: {iteration}

Candidate (JSON):
{candidate_json}

Return JSON matching the ReviewIteration schema below.

Review schema:
{review_schema}
"""


_DEFAULT_TEMPLATES = (
    PromptTemplate(
        name="creator_default",
        template=_CREATOR_TEMPLATE,
        description="Default Creator prompt for local CLI runners.",
    ),
    PromptTemplate(
        name="reviewer_default",
        template=_REVIEWER_TEMPLATE,
        description="Default Reviewer prompt for local CLI runners.",
    ),
)


def default_prompt_registry() -> PromptRegistry:
    return PromptRegistry(_DEFAULT_TEMPLATES)


def render_creator_prompt(
    *,
    renderer: PromptRenderer,
    inp: CreatorInput,
    glossary: Mapping[str, str] | Sequence[GlossaryEntry | Mapping[str, Any] | Sequence[str]] | None = None,
    few_shots: Sequence[FewShotExample | Mapping[str, Any]] | None = None,
) -> PromptRenderResult:
    context = {
        "asset_id": inp.asset_id,
        "iteration": str(inp.iteration),
        "previous_candidate_json": _json_block(_candidate_json(inp.previous_candidate)),
        "previous_review_json": _json_block(_review_json(inp.previous_review)),
        "candidate_schema": _json_block(candidate_json_schema()),
    }
    return renderer.render(
        name="creator_default",
        context=context,
        glossary=glossary,
        few_shots=few_shots,
    )


def render_reviewer_prompt(
    *,
    renderer: PromptRenderer,
    inp: ReviewerInput,
    glossary: Mapping[str, str] | Sequence[GlossaryEntry | Mapping[str, Any] | Sequence[str]] | None = None,
    few_shots: Sequence[FewShotExample | Mapping[str, Any]] | None = None,
) -> PromptRenderResult:
    context = {
        "asset_id": inp.asset_id,
        "iteration": str(inp.iteration),
        "candidate_json": _json_block(inp.candidate.model_dump(mode="json")),
        "review_schema": _json_block(review_iteration_json_schema()),
    }
    return renderer.render(
        name="reviewer_default",
        context=context,
        glossary=glossary,
        few_shots=few_shots,
    )


_SAFE_REF_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _prompt_ref(template: str, text: str) -> str:
    digest = sha256(f"{template}\n{text}".encode()).hexdigest()
    safe_template = _safe_ref_token(template)
    return f"{safe_template}_{digest[:12]}"


def _safe_ref_token(value: str) -> str:
    cleaned = _SAFE_REF_RE.sub("_", value).strip("._-")
    if not cleaned:
        raise ValueError("prompt template name cannot be empty after sanitization")
    return cleaned


def _normalize_glossary(
    glossary: Mapping[str, str] | Sequence[GlossaryEntry | Mapping[str, Any] | Sequence[str]] | None,
) -> tuple[GlossaryEntry, ...]:
    if glossary is None:
        return ()
    if isinstance(glossary, Mapping):
        items = sorted(glossary.items(), key=lambda item: str(item[0]))
        return tuple(GlossaryEntry(term=str(term), definition=str(defn)) for term, defn in items)
    if isinstance(glossary, (str, bytes)):
        raise TypeError("Glossary must be a mapping or sequence of entries")
    entries = [GlossaryEntry.from_value(item) for item in glossary]
    return tuple(entries)


def _normalize_few_shots(
    few_shots: Sequence[FewShotExample | Mapping[str, Any]] | str | bytes | None,
) -> tuple[FewShotExample, ...]:
    if few_shots is None:
        return ()
    if isinstance(few_shots, (str, bytes)):
        raise TypeError("Few-shot examples must be a sequence of entries")
    return tuple(FewShotExample.from_value(item) for item in few_shots)


def _render_glossary(entries: Sequence[GlossaryEntry]) -> str:
    lines = ["Glossary:"]
    for entry in entries:
        lines.append(f"- {entry.term}: {entry.definition}")
    return "\n".join(lines)


def _render_few_shots(examples: Sequence[FewShotExample]) -> str:
    lines = ["Few-shot examples:"]
    for idx, example in enumerate(examples, start=1):
        lines.append(f"Example {idx}:")
        lines.append("User:")
        lines.append(example.input_text)
        lines.append("Assistant:")
        lines.append(example.output_text)
    return "\n".join(lines)


def _json_block(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _candidate_json(candidate: Candidate | None) -> Any:
    if candidate is None:
        return None
    return candidate.model_dump(mode="json")


def _review_json(review: ReviewIteration | None) -> Any:
    if review is None:
        return None
    return review.model_dump(mode="json")
