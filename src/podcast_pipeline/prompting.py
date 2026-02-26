from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from podcast_pipeline.domain.models import Candidate, ProvenanceRef, ReviewIteration
from podcast_pipeline.protocol_schemas import (
    asset_candidates_response_json_schema,
    candidate_json_schema,
    chunk_summary_json_schema,
    episode_summary_json_schema,
    review_iteration_json_schema,
)
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
{episode_context}
Previous candidate (JSON):
{previous_candidate_json}

Previous review (JSON):
{previous_review_json}

Return a JSON object (no markdown fencing) with fields:
- applied (bool): true if you incorporated feedback from the previous review
- done (bool): true if the candidate is ready for publication
  (set true when no remaining issues exist, or on first iteration
  if the draft is already strong)
- candidate (Candidate schema below)

Candidate schema:
{candidate_schema}
"""


_REVIEWER_TEMPLATE = """You are the Reviewer agent for podcast copy.

Asset id: {asset_id}
Iteration: {iteration}
{episode_context}
Candidate (JSON):
{candidate_json}

Return a JSON object (no markdown fencing) matching the ReviewIteration schema below.

Review schema:
{review_schema}
"""


_CHUNK_SUMMARY_TEMPLATE = """You are a summarization assistant for a German-language podcast.

Summarize the following transcript chunk. The podcast is in German;
keep all output in German.

Chunk id: {chunk_id}

Transcript chunk:
{chunk_text}

Return a JSON object (no markdown fencing) matching the schema below.

ChunkSummary schema:
{chunk_summary_schema}
"""


_EPISODE_SUMMARY_TEMPLATE = """You are a summarization assistant for a German-language podcast.

You are given summaries for every chunk of one episode transcript.
Synthesize them into a single episode-level summary. The podcast is
in German; keep all output in German.

Chunk summaries (JSON array):
{chunk_summaries_json}

Return a JSON object (no markdown fencing) matching the schema below.

EpisodeSummary schema:
{episode_summary_schema}
"""


_ASSET_CANDIDATES_TEMPLATE = """You are a copywriting assistant for a German-language podcast.

Generate {num_candidates} candidate texts for the asset type described below.
The podcast is in German; keep all output in German unless the asset type
explicitly requires a different language (e.g. slugs or keywords may be in English).

Asset type: {asset_id}
{asset_guidance}

Episode summary:
{episode_summary_markdown}

Key points:
{key_points}

Topics:
{topics}

Chapters:
{chapters}

Return a JSON object (no markdown fencing) matching the response schema below.
The "candidates" array must contain exactly {num_candidates} objects.

Response schema:
{response_schema}
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
    PromptTemplate(
        name="chunk_summary",
        template=_CHUNK_SUMMARY_TEMPLATE,
        description="Summarize one transcript chunk via LLM.",
    ),
    PromptTemplate(
        name="episode_summary",
        template=_EPISODE_SUMMARY_TEMPLATE,
        description="Reduce chunk summaries into an episode-level summary.",
    ),
    PromptTemplate(
        name="asset_candidates",
        template=_ASSET_CANDIDATES_TEMPLATE,
        description="Generate N candidates for one asset type.",
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
    episode_context: str | None = None,
) -> PromptRenderResult:
    context = {
        "asset_id": inp.asset_id,
        "iteration": str(inp.iteration),
        "episode_context": episode_context or "",
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
    episode_context: str | None = None,
) -> PromptRenderResult:
    context = {
        "asset_id": inp.asset_id,
        "iteration": str(inp.iteration),
        "episode_context": episode_context or "",
        "candidate_json": _json_block(inp.candidate.model_dump(mode="json")),
        "review_schema": _json_block(review_iteration_json_schema()),
    }
    return renderer.render(
        name="reviewer_default",
        context=context,
        glossary=glossary,
        few_shots=few_shots,
    )


_MAX_TRANSCRIPT_EXCERPT_CHARS = 4000


def render_episode_context(
    *,
    summary: str | None = None,
    key_points: Sequence[str] | None = None,
    chapters: str | None = None,
    transcript_excerpt: str | None = None,
    max_transcript_chars: int = _MAX_TRANSCRIPT_EXCERPT_CHARS,
) -> str:
    """Render episode context (summary, chapters, transcript) into a text block for prompts."""
    sections: list[str] = []

    if summary:
        sections.append(f"Episode summary:\n{summary.strip()}")

    if key_points:
        bullet_lines = "\n".join(f"- {point}" for point in key_points)
        sections.append(f"Key points:\n{bullet_lines}")

    if chapters:
        sections.append(f"Chapters:\n{chapters.strip()}")

    if transcript_excerpt:
        excerpt = transcript_excerpt.strip()
        if len(excerpt) > max_transcript_chars:
            excerpt = excerpt[:max_transcript_chars] + "\n[...truncated]"
        sections.append(f"Transcript excerpt:\n{excerpt}")

    if not sections:
        return ""

    return "\n\n".join(sections)


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


def render_chunk_summary_prompt(
    *,
    renderer: PromptRenderer,
    chunk_id: int,
    chunk_text: str,
) -> PromptRenderResult:
    context = {
        "chunk_id": str(chunk_id),
        "chunk_text": chunk_text,
        "chunk_summary_schema": _json_block(chunk_summary_json_schema()),
    }
    return renderer.render(name="chunk_summary", context=context)


def render_episode_summary_prompt(
    *,
    renderer: PromptRenderer,
    chunk_summaries_json: str,
) -> PromptRenderResult:
    context = {
        "chunk_summaries_json": chunk_summaries_json,
        "episode_summary_schema": _json_block(episode_summary_json_schema()),
    }
    return renderer.render(name="episode_summary", context=context)


def render_asset_candidates_prompt(
    *,
    renderer: PromptRenderer,
    asset_id: str,
    asset_guidance: str,
    episode_summary_markdown: str,
    key_points: Sequence[str],
    topics: Sequence[str],
    chapters: Sequence[str],
    num_candidates: int,
) -> PromptRenderResult:
    context = {
        "asset_id": asset_id,
        "asset_guidance": asset_guidance,
        "episode_summary_markdown": episode_summary_markdown,
        "key_points": "\n".join(f"- {p}" for p in key_points) if key_points else "(none)",
        "topics": "\n".join(f"- {t}" for t in topics) if topics else "(none)",
        "chapters": "\n".join(f"- {c}" for c in chapters) if chapters else "(none)",
        "num_candidates": str(num_candidates),
        "response_schema": _json_block(asset_candidates_response_json_schema(num_candidates=num_candidates)),
    }
    return renderer.render(name="asset_candidates", context=context)
