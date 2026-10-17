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
from podcast_pipeline.tag_assets import TAG_ASSET_IDS, TagSettings, postprocess_tag_content, tag_few_shots

_TAG_RULES = (
    "Tag rules:\n"
    "- Each tag is a short, specific search term (1-3 words): technologies, projects, libraries, "
    "tools, PEPs, Python versions (e.g. python3.13) or well-defined concepts discussed in depth.\n"
    "- Prefer the established spelling of technical terms (usually English, lowercase unless it is "
    "a proper name); use German only when no common English term exists.\n"
    "- Do not include show-level tags that fit every episode (podcast name, 'Podcast', "
    "'Programmierung', 'Softwareentwicklung'), language tags ('Deutsch', 'German'), or names of "
    "hosts or guests.\n"
    "- Do not include generic summary phrases (e.g. 'Neue Features', 'What's New', "
    "'Fehlermeldungen', 'Best Practices').\n"
    "- The Topics list below is input context, not a tag list; do not copy it verbatim.\n"
    "- If few-shot examples follow the response schema, each pairs a past episode (User) with "
    "its published tags (Assistant): match their length and style, tag this episode's content, "
    "and still answer with the JSON response schema."
)

_ASSET_GUIDANCE: dict[str, str] = {
    AssetKind.description: (
        "Write a full episode description in German. Use markdown formatting "
        "(headings, bullet lists, bold). Cover the main topics and key takeaways."
    ),
    AssetKind.shownotes: (
        "Write link-first shownotes in German using markdown. The output should be mostly "
        "a curated list of links (not long prose). Create a section '## Links' and provide "
        "many concrete markdown links in the form '- [Label](https://...) - short context'. "
        "Include links to tools, libraries, articles, people, and resources mentioned or "
        "strongly implied by the episode. Add chapter notes only as a secondary section."
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
        "Generate CMS tags for this episode. Return markdown only as a bullet list "
        "with one tag per line (format: '- tag'). No prose, no explanations, no sentences.\n" + _TAG_RULES
    ),
    AssetKind.audio_tags: (
        "Generate ID3/audio metadata tags for this episode. Return markdown only as a bullet "
        "list with one short tag per line (format: '- tag'). No prose or commentary.\n" + _TAG_RULES
    ),
    AssetKind.itunes_keywords: (
        "Generate iTunes keywords. Return only a comma-separated keyword list (single line), "
        "with no heading and no extra prose.\n" + _TAG_RULES
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


def _tag_count_target(max_tags: int) -> str:
    high = min(10, max_tags)
    low = min(6, high)
    return f"{low}-{high} tags" if low < high else f"{high} tags"


def _asset_guidance(asset_id: str, *, tag_settings: TagSettings) -> str:
    guidance = _ASSET_GUIDANCE.get(asset_id, f"Generate content for {asset_id}.")
    if asset_id in TAG_ASSET_IDS:
        guidance += (
            f"\n- Return {_tag_count_target(tag_settings.max_tags)}, never more than {tag_settings.max_tags}. "
            "Fewer precise tags beat many vague ones."
        )
    return guidance


def _parse_raw_candidates(
    payload: dict[str, Any],
    *,
    asset_id: str,
    candidates_per_asset: int,
    provenance_prefix: str,
    tag_settings: TagSettings,
    hosts: Sequence[str] | None,
) -> list[Candidate]:
    """Validate and parse raw candidate dicts from a drafter response.

    Tag-like assets are passed through the deterministic tag post-filter.
    """
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
        raw = dict(raw)
        raw.pop("candidate_id", None)
        raw.setdefault("asset_id", asset_id)
        if raw.get("asset_id") != asset_id:
            raise RuntimeError(
                f"Drafter candidate {idx} for {asset_id} returned wrong asset_id: {raw.get('asset_id')!r}"
            )
        raw.setdefault("format", TextFormat.markdown.value)
        content = raw.get("content")
        if isinstance(content, str):
            raw["content"] = postprocess_tag_content(asset_id, content, settings=tag_settings, hosts=hosts)
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
            reference = Path(raw_chapters)
            error = (
                "Invalid chapters input (inputs.chapters in episode.yaml): "
                "expected a relative regular file inside the episode workspace"
            )
            if reference.is_absolute() or ".." in reference.parts:
                raise RuntimeError(error)
            try:
                root = layout.root.resolve(strict=True)
                chapters_path = (root / reference).resolve(strict=True)
                if not chapters_path.is_relative_to(root) or not chapters_path.is_file():
                    raise RuntimeError(error)
            except (OSError, ValueError, RuntimeError) as exc:
                raise RuntimeError(error) from exc
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
    from podcast_pipeline.tag_assets import load_tag_settings

    episode_summary, chapters, hosts = _load_workspace_context(workspace)
    tag_settings = load_tag_settings(workspace=workspace)

    bundle = load_agent_cli_bundle(workspace=workspace)
    renderer = PromptRenderer(default_prompt_registry())
    runner = DrafterCliRunner(
        config=bundle.drafter,
        timeout_seconds=timeout_seconds,
        cwd=str(workspace),
    )

    guidance = _asset_guidance(asset_id, tag_settings=tag_settings)
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
        few_shots=tag_few_shots(asset_id, settings=tag_settings, topics=episode_summary.topics, hosts=hosts),
    )
    typer.echo(f"  Generating candidates for {asset_id}...", err=True)
    payload = runner.run(prompt.text)

    return _parse_raw_candidates(
        payload,
        asset_id=asset_id,
        candidates_per_asset=candidates_per_asset,
        provenance_prefix="regen_v1",
        tag_settings=tag_settings,
        hosts=hosts,
    )


def generate_draft_candidates_llm(
    *,
    episode_summary: EpisodeSummary,
    chapters: Sequence[str],
    candidates_per_asset: int,
    runner: DrafterRunner,
    renderer: PromptRenderer,
    hosts: Sequence[str] | None = None,
    tag_settings: TagSettings | None = None,
) -> dict[str, list[Candidate]]:
    """Generate draft candidates for all asset types via LLM calls.

    One LLM call per asset type, each producing *candidates_per_asset* candidates.
    *tag_settings* defaults to the built-in tag rules without few-shot examples.
    """
    settings = tag_settings if tag_settings is not None else TagSettings()
    assets: dict[str, list[Candidate]] = {}

    for kind in AssetKind:
        asset_id = kind.value
        guidance = _asset_guidance(asset_id, tag_settings=settings)

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
            few_shots=tag_few_shots(asset_id, settings=settings, topics=episode_summary.topics, hosts=hosts),
        )
        typer.echo(f"  Generating candidates for {asset_id}...", err=True)
        payload = runner.run(prompt.text)

        assets[asset_id] = _parse_raw_candidates(
            payload,
            asset_id=asset_id,
            candidates_per_asset=candidates_per_asset,
            provenance_prefix="asset_v1",
            tag_settings=settings,
            hosts=hosts,
        )

    return assets
