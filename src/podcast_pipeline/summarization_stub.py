from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from podcast_pipeline import summary_inputs
from podcast_pipeline.control_json import load_control_json
from podcast_pipeline.domain.intermediate_formats import ChunkSummary, EpisodeSummary
from podcast_pipeline.domain.models import ProvenanceRef
from podcast_pipeline.file_inputs import read_regular_bytes, sha256_regular_file
from podcast_pipeline.markdown_html import markdown_to_deterministic_html
from podcast_pipeline.transcript_chunker import CHUNK_MANIFEST_MAX_BYTES
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, atomic_write_text, preserve_files


@dataclass(frozen=True)
class StubSummarizerConfig:
    max_bullets_per_chunk: int = 5
    max_episode_key_points: int = 12


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'_-]+")
_SUMMARY_MARKER_BYTES = 4096
_SUMMARY_MARKDOWN_BYTES = 32 * 1024 * 1024
_SUMMARY_HTML_BYTES = 64 * 1024 * 1024


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


def _summary_artifact_digest(path: Path, text: str, *, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) > max_bytes:
        raise ValueError(f"{path.name} exceeds the {max_bytes}-byte summary artifact limit")
    return hashlib.sha256(data).hexdigest()


def write_episode_summary_artifacts(
    *,
    layout: EpisodeWorkspaceLayout,
    episode_summary: EpisodeSummary,
) -> tuple[Path, Path, Path]:
    json_path = layout.episode_summary_json_path()
    md_path = layout.episode_summary_markdown_path()
    html_path = layout.episode_summary_html_path()

    dumped = json.dumps(episode_summary.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"

    md_text = episode_summary.summary_markdown
    if not md_text.endswith("\n"):
        md_text += "\n"

    json_digest = _summary_artifact_digest(json_path, dumped, max_bytes=summary_inputs.MAX_SUMMARY_BYTES)
    try:
        load_control_json(dumped)
    except ValueError:
        raise ValueError(f"{json_path.name} exceeds the summary JSON admission limits or is invalid") from None
    md_digest = _summary_artifact_digest(md_path, md_text, max_bytes=_SUMMARY_MARKDOWN_BYTES)
    html_text = _wrap_html_document(markdown_to_deterministic_html(md_text))
    html_digest = _summary_artifact_digest(html_path, html_text, max_bytes=_SUMMARY_HTML_BYTES)
    files = {json_path: dumped, md_path: md_text, html_path: html_text}
    marker = layout.episode_summary_dir / "complete.json"
    chunk_manifest = layout.transcript_chunks_dir / "complete.json"
    manifest = {
        "version": 1,
        "chunks_sha256": hashlib.sha256(chunk_manifest.read_bytes()).hexdigest() if chunk_manifest.exists() else None,
        "files": {json_path.name: json_digest, md_path.name: md_digest, html_path.name: html_digest},
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with preserve_files([*files, marker]):
        for path, text in files.items():
            atomic_write_text(path, text)
        atomic_write_text(marker, json.dumps(manifest, sort_keys=True) + "\n")

    return json_path, md_path, html_path


def load_complete_episode_summary(layout: EpisodeWorkspaceLayout) -> EpisodeSummary | None:
    """Return the model from the exact JSON bytes bound by a complete receipt."""
    try:
        marker = load_control_json(
            read_regular_bytes(layout.episode_summary_dir / "complete.json", max_bytes=_SUMMARY_MARKER_BYTES)
        )
        if not isinstance(marker, dict) or type(marker.get("version")) is not int or marker["version"] != 1:
            return None
        json_path = layout.episode_summary_json_path()
        markdown_path = layout.episode_summary_markdown_path()
        html_path = layout.episode_summary_html_path()
        files = marker.get("files")
        if not isinstance(files, dict) or set(files) != {json_path.name, markdown_path.name, html_path.name}:
            return None
        chunks_digest = sha256_regular_file(
            layout.transcript_chunks_dir / "complete.json", max_bytes=CHUNK_MANIFEST_MAX_BYTES
        )
        if marker.get("chunks_sha256") != chunks_digest:
            return None
        summary_json = read_regular_bytes(json_path, max_bytes=summary_inputs.MAX_SUMMARY_BYTES)
        if files[json_path.name] != hashlib.sha256(summary_json).hexdigest():
            return None
        for path, limit in ((markdown_path, _SUMMARY_MARKDOWN_BYTES), (html_path, _SUMMARY_HTML_BYTES)):
            if files[path.name] != sha256_regular_file(path, max_bytes=limit):
                return None
        return EpisodeSummary.model_validate(load_control_json(summary_json))
    except (OSError, ValueError, RecursionError):
        return None


def episode_summary_is_complete(layout: EpisodeWorkspaceLayout) -> bool:
    return load_complete_episode_summary(layout) is not None
