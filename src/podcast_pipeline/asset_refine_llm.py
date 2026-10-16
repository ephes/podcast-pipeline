"""Refine one asset text with a producer instruction via the drafter agent.

Unlike regeneration, which drafts new candidates from the episode summary,
refinement rewrites a given text (a candidate or the producer's custom text)
following an instruction such as "kürzer" or "ohne Emojis". The result is a new
candidate that records the instruction in its provenance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import typer

from podcast_pipeline.domain.models import AssetKind, Candidate, ProvenanceRef, TextFormat
from podcast_pipeline.drafter_runner import DrafterRunner
from podcast_pipeline.pick_core import custom_selection_format, normalize_custom_selection_text

REFINE_PROVENANCE_KIND = "llm_refine"
MAX_INSTRUCTION_CHARS = 2000

_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["content"],
    "properties": {"content": {"type": "string", "description": "The complete revised text."}},
    "additionalProperties": False,
}

_REFINE_TEMPLATE = """You are a copywriting assistant for a German-language podcast.

Revise the existing text for the asset type below so that it follows the producer's instruction.
Change only what the instruction asks for and keep everything else (facts, links, structure, language) as it is.
The podcast is in German; keep the text in German unless the asset type or the instruction requires otherwise.

Asset type: {asset_id}
{asset_guidance}
{format_rule}{editorial_notes}
Producer instruction:
{instruction}

Existing text:
<<<
{source_text}
>>>
{episode_context}
Return a JSON object (no markdown fencing) matching the response schema below, with the complete revised text
in "content".

Response schema:
{response_schema}
"""


@dataclass(frozen=True)
class RefineRequest:
    asset_id: str
    instruction: str
    source_text: str
    based_on_candidate_id: UUID | None = None
    editorial_notes: str | None = None


def build_refine_prompt(
    *,
    asset_id: str,
    source_text: str,
    instruction: str,
    editorial_notes: str | None = None,
    episode_summary_markdown: str | None = None,
) -> str:
    from podcast_pipeline.asset_candidates_llm import _ASSET_GUIDANCE

    fmt = custom_selection_format(asset_id)
    if fmt == TextFormat.plain:
        format_rule = "Return plain text only: no Markdown, no heading, no surrounding quotes.\n"
    else:
        format_rule = "Return Markdown.\n"
    if asset_id == AssetKind.slug.value:
        format_rule += "A slug uses only lowercase letters a-z, digits and single hyphens between words.\n"
    notes = f"\nStanding editorial notes for this asset:\n{editorial_notes.strip()}\n" if editorial_notes else ""
    context = ""
    if episode_summary_markdown:
        context = f"\nEpisode summary (context only):\n{episode_summary_markdown.strip()}\n"
    return _REFINE_TEMPLATE.format(
        asset_id=asset_id,
        asset_guidance=_ASSET_GUIDANCE.get(asset_id, f"Text for {asset_id}."),
        format_rule=format_rule,
        editorial_notes=notes,
        instruction=instruction.strip(),
        source_text=source_text.strip(),
        episode_context=context,
        response_schema=json.dumps(_RESPONSE_SCHEMA, indent=2),
    )


def validate_instruction(instruction: str) -> str:
    """Return the stripped instruction; raise ValueError when it is empty or too long."""
    stripped = instruction.strip()
    if not stripped:
        raise ValueError("instruction must not be empty")
    if len(stripped) > MAX_INSTRUCTION_CHARS:
        raise ValueError(f"instruction must be at most {MAX_INSTRUCTION_CHARS} characters")
    return stripped


def parse_refined_candidate(
    payload: dict[str, object],
    *,
    asset_id: str,
    instruction: str,
    based_on_candidate_id: UUID | None,
) -> Candidate:
    """Turn the drafter's JSON answer into a candidate in the asset's natural format."""
    content = payload.get("content")
    if not isinstance(content, str):
        # Tolerate the regular candidates shape: {"candidates": [{"content": ...}]}.
        candidates = payload.get("candidate") or payload.get("candidates")
        first = candidates[0] if isinstance(candidates, list) and candidates else candidates
        content = first.get("content") if isinstance(first, dict) else None
    if not isinstance(content, str):
        raise RuntimeError(f"Drafter response for refining {asset_id} has no 'content' string")
    try:
        normalized = normalize_custom_selection_text(asset_id, content)
    except ValueError as exc:
        raise RuntimeError(f"Drafter returned unusable text for {asset_id}: {exc}") from exc

    candidate = Candidate(asset_id=asset_id, format=custom_selection_format(asset_id), content=normalized)
    source = str(based_on_candidate_id) if based_on_candidate_id is not None else "text"
    provenance = ProvenanceRef(
        kind=REFINE_PROVENANCE_KIND,
        ref=f"refine_v1:{source}",
        created_at=candidate.created_at,
        metadata={
            "instruction": instruction,
            "based_on_candidate_id": str(based_on_candidate_id) if based_on_candidate_id is not None else None,
        },
    )
    return candidate.model_copy(update={"provenance": [provenance]})


def refine_asset_text(
    *,
    workspace: Path,
    request: RefineRequest,
    runner: DrafterRunner | None = None,
    timeout_seconds: float | None = None,
) -> Candidate:
    """Ask the drafter to rewrite the request's text following its instruction; return the new candidate.

    The candidate is not written to the workspace; callers decide where it goes.
    """
    instruction = validate_instruction(request.instruction)
    if not request.source_text.strip():
        raise ValueError("There is no text to refine")
    asset_id = request.asset_id

    if runner is None:
        from podcast_pipeline.agent_cli_config import load_agent_cli_bundle
        from podcast_pipeline.drafter_runner import DrafterCliRunner

        bundle = load_agent_cli_bundle(workspace=workspace)
        runner = DrafterCliRunner(config=bundle.drafter, timeout_seconds=timeout_seconds, cwd=str(workspace))

    prompt = build_refine_prompt(
        asset_id=asset_id,
        source_text=request.source_text,
        instruction=instruction,
        editorial_notes=request.editorial_notes,
        episode_summary_markdown=_episode_summary_markdown(workspace),
    )
    typer.echo(f"  Refining {asset_id}: {instruction}", err=True)
    payload = runner.run(prompt)
    return parse_refined_candidate(
        payload,
        asset_id=asset_id,
        instruction=instruction,
        based_on_candidate_id=request.based_on_candidate_id,
    )


def _episode_summary_markdown(workspace: Path) -> str | None:
    """Episode summary as optional context: refinement works without a summary."""
    from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
    from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout

    path = EpisodeWorkspaceLayout(root=workspace).episode_summary_json_path()
    if not path.exists():
        return None
    try:
        return EpisodeSummary.model_validate(json.loads(path.read_text(encoding="utf-8"))).summary_markdown
    except (OSError, ValueError):
        return None


def build_refine_request(
    *,
    asset_id: str,
    instruction: str,
    candidates: list[Candidate],
    candidate_id: str | None,
    text: str | None,
    current_selection_text: str | None,
    editorial_notes: str | None = None,
) -> RefineRequest:
    """Validate a refine request from a UI; raise ValueError for bad input.

    The text to refine is the given candidate, else the given (editor) text, else the current selection.
    """
    instruction = validate_instruction(instruction)
    source_text, based_on = _resolve_refine_source(
        candidates=candidates,
        candidate_id=candidate_id,
        text=text,
        current_selection_text=current_selection_text,
    )
    return RefineRequest(
        asset_id=asset_id,
        instruction=instruction,
        source_text=source_text,
        based_on_candidate_id=based_on,
        editorial_notes=editorial_notes or None,
    )


def _resolve_refine_source(
    *,
    candidates: list[Candidate],
    candidate_id: str | None,
    text: str | None,
    current_selection_text: str | None,
) -> tuple[str, UUID | None]:
    if candidate_id is not None and text is not None:
        raise ValueError("Pass either candidate_id or text, not both")
    if candidate_id is not None:
        try:
            candidate_uuid = UUID(candidate_id)
        except ValueError as exc:
            raise ValueError(f"Invalid candidate_id: {candidate_id}") from exc
        for candidate in candidates:
            if candidate.candidate_id == candidate_uuid:
                return candidate.content, candidate_uuid
        raise ValueError(f"candidate_id {candidate_id} not found")
    source = text if text is not None else current_selection_text
    if source is None or not source.strip():
        raise ValueError("There is no text to refine: choose a candidate or enter text first")
    return source, None


def refine_instruction(candidate: Candidate) -> str | None:
    """The producer instruction a refined candidate was made with, if any."""
    for ref in candidate.provenance:
        if ref.kind == REFINE_PROVENANCE_KIND:
            instruction = ref.metadata.get("instruction")
            return instruction if isinstance(instruction, str) else None
    return None
