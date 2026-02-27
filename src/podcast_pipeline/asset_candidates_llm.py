from __future__ import annotations

from collections.abc import Sequence

import typer

from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.domain.models import AssetKind, Candidate, ProvenanceRef, TextFormat
from podcast_pipeline.drafter_runner import DrafterRunner  # noqa: TC001
from podcast_pipeline.prompting import PromptRenderer, render_asset_candidates_prompt

_ASSET_GUIDANCE: dict[str, str] = {
    AssetKind.description: (
        "Write a full episode description in German. Use markdown formatting "
        "(headings, bullet lists, bold). Cover the main topics and key takeaways."
    ),
    AssetKind.shownotes: (
        "Write structured shownotes in German with markdown. Include chapter-by-chapter "
        "notes, links, and resources mentioned in the episode."
    ),
    AssetKind.summary_short: (
        "Write a short summary (2-3 sentences) in German suitable for RSS feeds and podcast directories."
    ),
    AssetKind.title_detail: (
        "Write a detailed episode title in German that includes the main topic. Keep it under 100 characters."
    ),
    AssetKind.title_seo: (
        "Write an SEO-optimized episode title in German with relevant keywords. Keep it under 70 characters."
    ),
    AssetKind.subtitle_auphonic: (
        "Write a short subtitle (one sentence) in German for the Auphonic metadata. Keep it under 255 characters."
    ),
    AssetKind.slug: (
        "Generate a URL-safe slug in lowercase English/German. Use hyphens to "
        "separate words. Keep it concise and descriptive."
    ),
    AssetKind.cms_tags: (
        "Generate a list of CMS tags (topics) for this episode. Mix German and "
        "English terms as appropriate. Return each tag as the content field."
    ),
    AssetKind.audio_tags: (
        "Generate ID3/audio metadata tags for this episode. Use short topic labels. "
        "Return each tag as the content field."
    ),
    AssetKind.itunes_keywords: (
        "Generate comma-separated iTunes keywords. Mix German and English terms. "
        "Return the full comma-separated string as the content field."
    ),
    AssetKind.mastodon: (
        "Write a Mastodon/social media post in German announcing the episode. "
        "Include relevant hashtags. Keep it under 500 characters."
    ),
    AssetKind.linkedin: (
        "Write a LinkedIn post in German announcing the episode. Include key takeaways and a professional tone."
    ),
    AssetKind.youtube_description: (
        "Write a YouTube video description in German. Include chapters with "
        "timestamps (use 00:00 format), key topics, and relevant links."
    ),
}


def generate_draft_candidates_llm(
    *,
    episode_summary: EpisodeSummary,
    chapters: Sequence[str],
    candidates_per_asset: int,
    runner: DrafterRunner,
    renderer: PromptRenderer,
    hosts: Sequence[str] | None = None,
) -> dict[str, list[Candidate]]:
    """Generate draft candidates for all asset types via LLM calls.

    One LLM call per asset type, each producing *candidates_per_asset* candidates.
    """
    assets: dict[str, list[Candidate]] = {}

    for kind in AssetKind:
        asset_id = kind.value
        guidance = _ASSET_GUIDANCE.get(asset_id, f"Generate content for {asset_id}.")

        prompt = render_asset_candidates_prompt(
            renderer=renderer,
            asset_id=asset_id,
            asset_guidance=guidance,
            episode_summary_markdown=episode_summary.summary_markdown,
            key_points=episode_summary.key_points,
            topics=episode_summary.topics,
            chapters=list(chapters),
            num_candidates=candidates_per_asset,
            hosts=hosts,
        )
        typer.echo(f"  Generating candidates for {asset_id}...", err=True)
        payload = runner.run(prompt.text)

        raw_candidates = payload.get("candidates")
        if not isinstance(raw_candidates, list):
            raise RuntimeError(f"Drafter response for {asset_id} missing 'candidates' array")
        if len(raw_candidates) != candidates_per_asset:
            raise RuntimeError(
                f"Drafter returned {len(raw_candidates)} candidates for {asset_id}, expected {candidates_per_asset}"
            )

        candidates: list[Candidate] = []
        for idx, raw in enumerate(raw_candidates):
            if not isinstance(raw, dict):
                raise RuntimeError(f"Drafter candidate {idx} for {asset_id} is not an object")
            raw.setdefault("asset_id", asset_id)
            if raw.get("asset_id") != asset_id:
                raise RuntimeError(
                    f"Drafter candidate {idx} for {asset_id} returned wrong asset_id: {raw.get('asset_id')!r}"
                )
            raw.setdefault("format", TextFormat.markdown.value)
            raw.setdefault(
                "provenance",
                [ProvenanceRef(kind="llm_drafter", ref=f"asset_v1:{idx + 1:02d}").model_dump(mode="json")],
            )
            candidates.append(Candidate.model_validate(raw))

        assets[asset_id] = candidates

    return assets
