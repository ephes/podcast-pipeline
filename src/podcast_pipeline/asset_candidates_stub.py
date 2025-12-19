from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.domain.models import (
    AssetKind,
    Candidate,
    ProvenanceRef,
    TextFormat,
)

_FAKE_UUID_NAMESPACE = UUID("00000000-0000-0000-0000-000000000000")
_DEFAULT_CREATED_AT = datetime(2000, 1, 1, 0, 0, 0, tzinfo=UTC)
_GENERATOR_VERSION = "stub_asset_generator_v1"


@dataclass(frozen=True)
class DraftCandidatesConfig:
    candidates_per_asset: int = 3
    created_at: datetime = _DEFAULT_CREATED_AT
    version: str = _GENERATOR_VERSION


def _deterministic_uuid(*, prefix: str, parts: Sequence[str]) -> UUID:
    name = ":".join([prefix, *parts])
    return uuid5(_FAKE_UUID_NAMESPACE, name)


def _rotated(items: Sequence[str], *, shift: int) -> list[str]:
    if not items:
        return []
    shift %= len(items)
    return list(items[shift:]) + list(items[:shift])


def _pick(items: Sequence[str], *, variant: int, limit: int) -> list[str]:
    if limit <= 0:
        return []
    return _rotated(items, shift=variant - 1)[:limit]


_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    lowered = text.strip().lower()
    lowered = _NON_SLUG_RE.sub("-", lowered)
    lowered = lowered.strip("-")
    return lowered or "episode"


def _render_chapters(chapters: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in chapters:
        line = raw.strip()
        if not line:
            continue
        cleaned.append(line)
    return cleaned


def _first(items: Sequence[str], *, default: str) -> str:
    for item in items:
        stripped = item.strip()
        if stripped:
            return stripped
    return default


def _join_bullets(items: Sequence[str]) -> str:
    if not items:
        return "- (none)\n"
    return "".join(f"- {item}\n" for item in items)


@dataclass(frozen=True)
class _RenderContext:
    variant: int
    episode_title: str
    summary_sentence: str
    title_topic_a: str
    title_topic_b: str
    key_points: Sequence[str]
    topics: Sequence[str]
    chapters: list[str]


def _render_description(context: _RenderContext) -> str:
    key_points_block = _join_bullets(
        _pick(context.key_points, variant=context.variant, limit=6),
    ).rstrip()
    chapters_block = _join_bullets(
        context.chapters or ["(no chapters provided)"],
    ).rstrip()
    topics_block = _join_bullets(
        _pick(context.topics, variant=context.variant, limit=10),
    ).rstrip()
    lines = [
        f"# Episode description (candidate {context.variant})",
        "",
        context.summary_sentence,
        "",
        "## Key points",
        "",
        key_points_block,
        "",
        "## Chapters",
        "",
        chapters_block,
        "",
        "## Topics",
        "",
        topics_block,
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _render_shownotes(context: _RenderContext) -> str:
    chapter_points = _join_bullets(
        _pick(context.key_points, variant=context.variant, limit=3),
    ).rstrip()
    chapter_sections: list[str] = []
    for chapter in context.chapters[:12]:
        chapter_sections.extend([f"## {chapter}", "", chapter_points, ""])

    if not chapter_sections:
        notes_points = _join_bullets(
            _pick(context.key_points, variant=context.variant, limit=6),
        ).rstrip()
        chapter_sections = ["## Notes", "", notes_points, ""]

    topics_block = _join_bullets(
        _pick(context.topics, variant=context.variant, limit=12),
    ).rstrip()
    lines = [
        f"# Shownotes (candidate {context.variant})",
        "",
        f"Episode: {context.episode_title}",
        "",
        *chapter_sections,
        "## Topics",
        "",
        topics_block,
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _render_summary_short(context: _RenderContext) -> str:
    point_a = _first(
        _pick(context.key_points, variant=context.variant, limit=1),
        default="A new episode is out.",
    )
    point_b = _first(
        _pick(context.key_points, variant=context.variant + 1, limit=1),
        default="We cover practical takeaways.",
    )
    match context.variant % 3:
        case 1:
            content = f"{point_a} {point_b}"
        case 2:
            content = f"In this episode: {point_a} Also: {point_b}"
        case _:
            content = f"{context.episode_title}: {point_a} {point_b}"
    return f"# Summary (short) (candidate {context.variant})\n\n{content}\n"


def _render_title_detail(context: _RenderContext) -> str:
    return (
        f"# Title (detail) (candidate {context.variant})\n\n{context.episode_title}: {context.title_topic_a.title()}\n"
    )


def _render_title_seo(context: _RenderContext) -> str:
    seo = f"{context.episode_title} — {context.title_topic_a.title()} tips"
    return f"# Title (SEO) (candidate {context.variant})\n\n{seo}\n"


def _render_subtitle_auphonic(context: _RenderContext) -> str:
    subtitle = _first(
        _pick(context.key_points, variant=context.variant, limit=1),
        default=context.episode_title,
    )
    return f"# Subtitle (Auphonic) (candidate {context.variant})\n\n{subtitle}\n"


def _render_slug(context: _RenderContext) -> str:
    slug = _slugify(f"{context.title_topic_a}-{context.title_topic_b}")
    return f"# Slug (candidate {context.variant})\n\n{slug}\n"


def _render_cms_tags(context: _RenderContext) -> str:
    topics_block = _join_bullets(
        _pick(context.topics, variant=context.variant, limit=12),
    ).rstrip()
    lines = [f"# CMS tags (candidate {context.variant})", "", topics_block, ""]
    return "\n".join(lines).rstrip() + "\n"


def _render_audio_tags(context: _RenderContext) -> str:
    topics_block = _join_bullets(
        _pick(context.topics, variant=context.variant + 1, limit=10),
    ).rstrip()
    lines = [f"# Audio tags (candidate {context.variant})", "", topics_block, ""]
    return "\n".join(lines).rstrip() + "\n"


def _render_itunes_keywords(context: _RenderContext) -> str:
    keywords = _pick(context.topics, variant=context.variant, limit=8)
    if not keywords:
        keywords = ["podcast", "audio", "automation"]
    keywords_text = ", ".join(keywords)
    return f"# iTunes keywords (candidate {context.variant})\n\n{keywords_text}\n"


def _render_mastodon(context: _RenderContext) -> str:
    hashtag = _slugify(context.title_topic_a).replace("-", "")
    lines = [
        f"# Mastodon (candidate {context.variant})",
        "",
        f"New episode: {context.episode_title}.",
        "",
        context.summary_sentence,
        "",
        f"#podcast #{hashtag}",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _render_linkedin(context: _RenderContext) -> str:
    key_points_block = _join_bullets(
        _pick(context.key_points, variant=context.variant, limit=4),
    ).rstrip()
    lines = [
        f"# LinkedIn (candidate {context.variant})",
        "",
        f"New episode: {context.episode_title}",
        "",
        context.summary_sentence,
        "",
        "Key takeaways:",
        "",
        key_points_block,
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _render_youtube_description(context: _RenderContext) -> str:
    chapters_block = _join_bullets(
        context.chapters or ["(no chapters provided)"],
    ).rstrip()
    topics_block = _join_bullets(
        _pick(context.topics, variant=context.variant, limit=12),
    ).rstrip()
    lines = [
        f"# YouTube description (candidate {context.variant})",
        "",
        context.summary_sentence,
        "",
        "## Chapters",
        "",
        chapters_block,
        "",
        "## Topics",
        "",
        topics_block,
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


_RENDERERS: dict[AssetKind, Callable[[_RenderContext], str]] = {
    AssetKind.description: _render_description,
    AssetKind.shownotes: _render_shownotes,
    AssetKind.summary_short: _render_summary_short,
    AssetKind.title_detail: _render_title_detail,
    AssetKind.title_seo: _render_title_seo,
    AssetKind.subtitle_auphonic: _render_subtitle_auphonic,
    AssetKind.slug: _render_slug,
    AssetKind.cms_tags: _render_cms_tags,
    AssetKind.audio_tags: _render_audio_tags,
    AssetKind.itunes_keywords: _render_itunes_keywords,
    AssetKind.mastodon: _render_mastodon,
    AssetKind.linkedin: _render_linkedin,
    AssetKind.youtube_description: _render_youtube_description,
}


def _render_asset_content(
    *,
    kind: AssetKind,
    variant: int,
    episode_summary: EpisodeSummary,
    chapters: Sequence[str],
) -> str:
    renderer = _RENDERERS.get(kind)
    if renderer is None:
        raise ValueError(f"Unsupported asset kind: {kind}")

    chapters_clean = _render_chapters(chapters)
    key_points = episode_summary.key_points
    topics = episode_summary.topics

    title_topic_a = _first(
        _pick(topics, variant=variant, limit=1),
        default="podcasting",
    )
    title_topic_b = _first(
        _pick(topics, variant=variant + 1, limit=1),
        default="automation",
    )
    episode_title = f"{title_topic_a} & {title_topic_b}".title()
    summary_sentence = _first(
        _pick(key_points, variant=variant, limit=1),
        default="A quick tour of the episode.",
    )

    context = _RenderContext(
        variant=variant,
        episode_title=episode_title,
        summary_sentence=summary_sentence,
        title_topic_a=title_topic_a,
        title_topic_b=title_topic_b,
        key_points=key_points,
        topics=topics,
        chapters=chapters_clean,
    )
    return renderer(context)


def generate_draft_candidates(
    *,
    episode_summary: EpisodeSummary,
    chapters: Sequence[str],
    config: DraftCandidatesConfig,
) -> dict[str, list[Candidate]]:
    if config.candidates_per_asset < 1:
        raise ValueError("candidates_per_asset must be >= 1")

    assets: dict[str, list[Candidate]] = {}
    for kind in AssetKind:
        candidates: list[Candidate] = []
        asset_id = kind.value
        for variant in range(1, config.candidates_per_asset + 1):
            candidate_id = _deterministic_uuid(
                prefix="draft_candidate",
                parts=[config.version, asset_id, str(variant)],
            )
            content = _render_asset_content(
                kind=kind,
                variant=variant,
                episode_summary=episode_summary,
                chapters=chapters,
            )
            candidates.append(
                Candidate(
                    candidate_id=candidate_id,
                    asset_id=asset_id,
                    format=TextFormat.markdown,
                    content=content,
                    created_at=config.created_at,
                    provenance=[
                        ProvenanceRef(
                            kind="stub_asset_generator",
                            ref=f"{config.version}:{variant:02d}",
                        ),
                    ],
                ),
            )
        assets[asset_id] = candidates
    return assets
