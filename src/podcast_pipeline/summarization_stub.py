from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from podcast_pipeline.domain.intermediate_formats import ChunkSummary, EpisodeSummary
from podcast_pipeline.domain.models import ProvenanceRef
from podcast_pipeline.markdown_html import markdown_to_deterministic_html
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


@dataclass(frozen=True)
class StubSummarizerConfig:
    max_bullets_per_chunk: int = 5
    max_episode_key_points: int = 12


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'_-]+")


def _first_non_empty_lines(text: str, *, limit: int) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        lines.append(stripped)
        if len(lines) >= limit:
            break
    return lines


def _bullets_from_text(text: str, *, limit: int) -> list[str]:
    bullets: list[str] = []
    for line in _first_non_empty_lines(text, limit=limit * 2):
        if len(bullets) >= limit:
            break
        normalized = line.lstrip("-*•").strip()
        if not normalized:
            continue
        if len(normalized) > 200:
            normalized = normalized[:197].rstrip() + "..."
        bullets.append(normalized)
    return bullets


def _entities_from_text(text: str, *, limit: int) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        if token.islower():
            continue
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        candidates.append(token)
        if len(candidates) >= limit:
            break
    return candidates


def summarize_transcript_chunks_stub(
    *,
    layout: EpisodeWorkspaceLayout,
    chunk_ids: list[int],
    config: StubSummarizerConfig,
) -> list[ChunkSummary]:
    summaries: list[ChunkSummary] = []

    for chunk_id in chunk_ids:
        chunk_path = layout.transcript_chunk_text_path(chunk_id)
        chunk_text = chunk_path.read_text(encoding="utf-8")

        excerpt_lines = _first_non_empty_lines(chunk_text, limit=2)
        excerpt = " ".join(excerpt_lines).strip() if excerpt_lines else "(empty chunk)"

        summary = ChunkSummary(
            chunk_id=chunk_id,
            summary_markdown="\n".join(
                [
                    f"## Chunk {chunk_id}",
                    "",
                    excerpt,
                    "",
                    "",
                ],
            ),
            bullets=_bullets_from_text(chunk_text, limit=config.max_bullets_per_chunk),
            entities=_entities_from_text(chunk_text, limit=12),
            provenance=[ProvenanceRef(kind="stub_summarizer", ref="chunk_v1")],
        )
        summaries.append(summary)

        out_path = layout.chunk_summary_json_path(chunk_id)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        dumped = json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        out_path.write_text(dumped, encoding="utf-8")

    return summaries


def _unique_lowered(
    items: list[str] | tuple[str, ...],
    *,
    limit: int,
) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for item in items:
        lowered = item.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def _collect_bullets(chunk_summaries: list[ChunkSummary]) -> list[str]:
    bullets: list[str] = []
    for chunk in chunk_summaries:
        bullets.extend(chunk.bullets)
    return bullets


def _collect_entities(chunk_summaries: list[ChunkSummary]) -> list[str]:
    entities: list[str] = []
    for chunk in chunk_summaries:
        entities.extend(chunk.entities)
    return entities


def _render_episode_summary_markdown(
    *,
    chunk_summaries: list[ChunkSummary],
    key_points: list[str],
) -> str:
    parts: list[str] = ["# Episode summary (stub)", ""]
    if key_points:
        parts.extend(["## Key points", ""])
        parts.extend([f"- {point}" for point in key_points])
        parts.append("")
    parts.append("## Chunk roll-up")
    parts.append("")
    for chunk in chunk_summaries:
        parts.append(chunk.summary_markdown.strip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def reduce_chunk_summaries_to_episode_summary_stub(
    *,
    chunk_summaries: list[ChunkSummary],
    config: StubSummarizerConfig,
) -> EpisodeSummary:
    key_points = _unique_lowered(
        _collect_bullets(chunk_summaries),
        limit=config.max_episode_key_points,
    )
    topics = _unique_lowered(_collect_entities(chunk_summaries), limit=12)
    summary_markdown = _render_episode_summary_markdown(
        chunk_summaries=chunk_summaries,
        key_points=key_points,
    )
    return EpisodeSummary(
        summary_markdown=summary_markdown,
        key_points=key_points,
        topics=topics,
        provenance=[ProvenanceRef(kind="stub_summarizer", ref="episode_v1")],
    )


def _wrap_html_document(fragment: str) -> str:
    fragment = fragment.rstrip()
    return (
        "\n".join(
            [
                "<!doctype html>",
                "<html>",
                "<body>",
                fragment,
                "</body>",
                "</html>",
            ],
        ).rstrip()
        + "\n"
    )


def write_episode_summary_artifacts(
    *,
    layout: EpisodeWorkspaceLayout,
    episode_summary: EpisodeSummary,
) -> tuple[Path, Path, Path]:
    json_path = layout.episode_summary_json_path()
    md_path = layout.episode_summary_markdown_path()
    html_path = layout.episode_summary_html_path()

    json_path.parent.mkdir(parents=True, exist_ok=True)

    dumped = json.dumps(episode_summary.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    json_path.write_text(dumped, encoding="utf-8")

    md_text = episode_summary.summary_markdown
    if not md_text.endswith("\n"):
        md_text += "\n"
    md_path.write_text(md_text, encoding="utf-8")

    html_text = _wrap_html_document(markdown_to_deterministic_html(md_text))
    html_path.write_text(html_text, encoding="utf-8")

    return json_path, md_path, html_path
