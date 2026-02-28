from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

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


def _parse_raw_candidates(
    payload: dict[str, Any],
    *,
    asset_id: str,
    candidates_per_asset: int,
    provenance_prefix: str,
) -> list[Candidate]:
    """Validate and parse raw candidate dicts from a drafter response."""
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
            [
                ProvenanceRef(
                    kind="llm_drafter",
                    ref=f"{provenance_prefix}:{idx + 1:02d}",
                ).model_dump(mode="json")
            ],
        )
        candidates.append(Candidate.model_validate(raw))
    return candidates


def _load_workspace_context(
    workspace: Path,
) -> tuple[EpisodeSummary, list[str], Sequence[str] | None]:
    """Load episode summary, chapters, and hosts from a workspace."""
    from podcast_pipeline.workspace_store import EpisodeWorkspaceStore

    store = EpisodeWorkspaceStore(workspace)
    layout = store.layout

    summary_path = layout.episode_summary_json_path()
    if not summary_path.exists():
        raise RuntimeError("Episode summary not found. Run the draft pipeline first.")

    episode_summary = EpisodeSummary.model_validate(
        json.loads(summary_path.read_text(encoding="utf-8")),
    )

    episode_yaml = store.read_episode_yaml()
    chapters: list[str] = []
    raw_inputs = episode_yaml.get("inputs")
    if isinstance(raw_inputs, dict):
        raw_chapters = raw_inputs.get("chapters")
        if isinstance(raw_chapters, str):
            chapters_path = layout.root / raw_chapters
            if chapters_path.exists():
                chapters = [
                    line.strip() for line in chapters_path.read_text(encoding="utf-8").splitlines() if line.strip()
                ][:200]

    hosts: Sequence[str] | None = None
    raw_hosts = episode_yaml.get("hosts")
    if isinstance(raw_hosts, list) and all(isinstance(h, str) for h in raw_hosts):
        hosts = raw_hosts

    return episode_summary, chapters, hosts


def generate_single_asset_candidates_llm(
    *,
    workspace: Path,
    asset_id: str,
    candidates_per_asset: int = 3,
    editorial_notes: str | None = None,
    timeout_seconds: float | None = None,
) -> list[Candidate]:
    """Generate candidates for a single asset type via LLM.

    Used by the dashboard for per-asset regeneration with editorial notes.
    """
    from podcast_pipeline.agent_cli_config import load_agent_cli_bundle
    from podcast_pipeline.drafter_runner import DrafterCliRunner
    from podcast_pipeline.prompting import PromptRenderer, default_prompt_registry

    episode_summary, chapters, hosts = _load_workspace_context(workspace)

    bundle = load_agent_cli_bundle(workspace=workspace)
    renderer = PromptRenderer(default_prompt_registry())
    runner = DrafterCliRunner(
        config=bundle.drafter,
        timeout_seconds=timeout_seconds,
        cwd=str(workspace),
    )

    guidance = _ASSET_GUIDANCE.get(asset_id, f"Generate content for {asset_id}.")
    if editorial_notes:
        guidance += f"\n\nEditorial notes from the producer:\n{editorial_notes}"

    prompt = render_asset_candidates_prompt(
        renderer=renderer,
        asset_id=asset_id,
        asset_guidance=guidance,
        episode_summary_markdown=episode_summary.summary_markdown,
        key_points=episode_summary.key_points,
        topics=episode_summary.topics,
        chapters=chapters,
        num_candidates=candidates_per_asset,
        hosts=hosts,
    )
    typer.echo(f"  Generating candidates for {asset_id}...", err=True)
    payload = runner.run(prompt.text)

    return _parse_raw_candidates(
        payload,
        asset_id=asset_id,
        candidates_per_asset=candidates_per_asset,
        provenance_prefix="regen_v1",
    )


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

        assets[asset_id] = _parse_raw_candidates(
            payload,
            asset_id=asset_id,
            candidates_per_asset=candidates_per_asset,
            provenance_prefix="asset_v1",
        )

    return assets
