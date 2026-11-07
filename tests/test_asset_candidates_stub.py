"""Offline contracts for the deterministic, workspace-local draft generator."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from podcast_pipeline.asset_candidates_stub import DraftCandidatesConfig, generate_draft_candidates
from podcast_pipeline.cms_draft import selected_slug
from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.domain.models import AssetKind, Candidate, TextFormat
from podcast_pipeline.tag_parsing import parse_tag_list
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


@pytest.fixture
def summary() -> EpisodeSummary:
    return EpisodeSummary(
        summary_markdown="UNUSED_ROLLUP_MARKER",
        key_points=["First practical insight.", "Second concrete lesson.", "Third useful finding."],
        topics=["alpha tools", "beta systems", "gamma workflows"],
    )


HEADINGS = {
    AssetKind.description: "Episode description",
    AssetKind.shownotes: "Shownotes",
    AssetKind.summary_short: "Summary (short)",
    AssetKind.title_detail: "Title (detail)",
    AssetKind.title_seo: "Title (SEO)",
    AssetKind.subtitle_auphonic: "Subtitle (Auphonic)",
    AssetKind.slug: "Slug",
    AssetKind.cms_tags: "CMS tags",
    AssetKind.audio_tags: "Audio tags",
    AssetKind.itunes_keywords: "iTunes keywords",
    AssetKind.mastodon: "Mastodon",
    AssetKind.linkedin: "LinkedIn",
    AssetKind.youtube_description: "YouTube description",
}


def body(candidate: Candidate) -> str:
    return candidate.content.split("\n\n", 1)[1].strip()


@pytest.mark.parametrize("kind", list(AssetKind))
def test_every_asset_has_format_and_provenance(kind: AssetKind, summary: EpisodeSummary) -> None:
    chapters = ["00:00 Opening marker", "04:30 Closing marker"]
    assets = generate_draft_candidates(
        episode_summary=summary, chapters=chapters, config=DraftCandidatesConfig(candidates_per_asset=3)
    )
    assert set(assets) == {kind.value for kind in AssetKind}
    assert set(HEADINGS) == set(AssetKind)
    candidates = assets[kind.value]
    assert len(candidates) == 3
    for i, candidate in enumerate(candidates):
        assert candidate.asset_id == kind.value
        assert candidate.format == TextFormat.markdown
        assert candidate.content.splitlines()[0] == f"# {HEADINGS[kind]} (candidate {i + 1})"
        assert "UNUSED_ROLLUP_MARKER" not in candidate.content
        assert [(p.kind, p.ref) for p in candidate.provenance] == [
            ("stub_asset_generator", f"stub_asset_generator_v1:{i + 1:02d}")
        ]


@pytest.mark.parametrize(
    "kind",
    [
        AssetKind.description,
        AssetKind.shownotes,
        AssetKind.youtube_description,
        AssetKind.mastodon,
        AssetKind.linkedin,
    ],
)
def test_long_copy_uses_relevant_sources(kind: AssetKind, summary: EpisodeSummary) -> None:
    candidates = generate_draft_candidates(
        episode_summary=summary,
        chapters=["00:00 Opening marker", "04:30 Closing marker"],
        config=DraftCandidatesConfig(),
    )[kind.value]
    titles = ["Alpha Tools & Beta Systems", "Beta Systems & Gamma Workflows", "Gamma Workflows & Alpha Tools"]
    topics = [
        ["alpha tools", "beta systems", "gamma workflows"],
        ["beta systems", "gamma workflows", "alpha tools"],
        ["gamma workflows", "alpha tools", "beta systems"],
    ]
    points = summary.key_points
    for i, candidate in enumerate(candidates):
        text = body(candidate)
        if kind in {AssetKind.description, AssetKind.youtube_description}:
            assert text.splitlines()[0] == points[i]
            assert "## Chapters\n\n- 00:00 Opening marker\n- 04:30 Closing marker" in text
            assert text.split("## Topics\n\n")[1].splitlines() == [f"- {t}" for t in topics[i]]
            if kind == AssetKind.description:
                assert "## Key points" in text
                assert all(point in text for point in points)
        elif kind == AssetKind.shownotes:
            assert text.startswith(f"Episode: {titles[i]}\n")
            assert "## 00:00 Opening marker" in text and "## 04:30 Closing marker" in text
            assert all(point in text for point in points)
            assert text.split("## Topics\n\n")[1].splitlines() == [f"- {t}" for t in topics[i]]
        elif kind == AssetKind.mastodon:
            assert text.startswith(f"New episode: {titles[i]}.\n")
            assert points[i] in text
            assert text.endswith(f"#podcast #{topics[i][0].replace(' ', '')}")
        elif kind == AssetKind.linkedin:
            assert text.startswith(f"New episode: {titles[i]}\n")
            assert points[i] in text
            assert "Key takeaways:" in text
            assert all(f"- {point}" in text for point in points)


@pytest.mark.parametrize(
    "kind",
    [
        AssetKind.summary_short,
        AssetKind.title_detail,
        AssetKind.title_seo,
        AssetKind.subtitle_auphonic,
        AssetKind.slug,
        AssetKind.cms_tags,
        AssetKind.audio_tags,
        AssetKind.itunes_keywords,
    ],
)
def test_short_copy_and_structured_readers(kind: AssetKind, summary: EpisodeSummary, tmp_path: Path) -> None:
    candidates = generate_draft_candidates(
        episode_summary=summary,
        chapters=[],
        config=DraftCandidatesConfig(),
    )[kind.value]
    titles = ["Alpha Tools & Beta Systems", "Beta Systems & Gamma Workflows", "Gamma Workflows & Alpha Tools"]
    topics = [
        ["alpha tools", "beta systems", "gamma workflows"],
        ["beta systems", "gamma workflows", "alpha tools"],
        ["gamma workflows", "alpha tools", "beta systems"],
    ]
    points = summary.key_points
    for i, candidate in enumerate(candidates):
        text = body(candidate)
        if kind == AssetKind.summary_short:
            assert (
                text
                == [
                    f"{points[0]} {points[1]}",
                    f"In this episode: {points[1]} Also: {points[2]}",
                    f"{titles[2]}: {points[2]} {points[0]}",
                ][i]
            )
        elif kind == AssetKind.title_detail:
            assert text == f"{titles[i]}: {topics[i][0].title()}"
        elif kind == AssetKind.title_seo:
            assert text == f"{titles[i]} — {topics[i][0].title()} tips"
        elif kind == AssetKind.subtitle_auphonic:
            assert text == points[i]
        elif kind == AssetKind.slug:
            path = EpisodeWorkspaceLayout(root=tmp_path).selected_text_path("slug", TextFormat.markdown)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(candidate.content, encoding="utf-8")
            assert (
                selected_slug(tmp_path)
                == ["alpha-tools-beta-systems", "beta-systems-gamma-workflows", "gamma-workflows-alpha-tools"][i]
            )
        elif kind in {AssetKind.cms_tags, AssetKind.audio_tags, AssetKind.itunes_keywords}:
            expected = topics[(i + 1) % 3] if kind == AssetKind.audio_tags else topics[i]
            assert parse_tag_list(candidate.content) == expected


def test_identity_dimensions_and_clock(summary: EpisodeSummary) -> None:
    def generate(config: DraftCandidatesConfig, source: EpisodeSummary = summary) -> list[Candidate]:
        return [
            candidate
            for candidates in generate_draft_candidates(episode_summary=source, chapters=[], config=config).values()
            for candidate in candidates
        ]

    original = generate(DraftCandidatesConfig(candidates_per_asset=5))
    ids = [c.candidate_id for c in original]
    assert len(set(ids)) == len(AssetKind) * 5
    assert {c.created_at for c in original} == {datetime(2000, 1, 1, tzinfo=UTC)}
    # A slot is independent of source content, clock and requested set size.
    changed_source = EpisodeSummary(summary_markdown="Changed", topics=["different"], key_points=["New point"])
    assert [c.candidate_id for c in generate(DraftCandidatesConfig(candidates_per_asset=5), changed_source)] == ids
    smaller = generate(DraftCandidatesConfig(candidates_per_asset=1))
    assert [c.candidate_id for c in smaller] == ids[::5]
    with_chapters = generate_draft_candidates(
        episode_summary=summary,
        chapters=["00:00 Different chapters"],
        config=DraftCandidatesConfig(candidates_per_asset=5),
    )
    assert [c.candidate_id for values in with_chapters.values() for c in values] == ids

    explicit_time = datetime(2024, 6, 7, 8, 9, 10, tzinfo=timezone(timedelta(hours=2)))
    timed = generate(DraftCandidatesConfig(candidates_per_asset=5, created_at=explicit_time))
    assert [c.candidate_id for c in timed] == ids
    assert all(c.created_at.isoformat() == "2024-06-07T08:09:10+02:00" for c in timed)
    versioned = generate(DraftCandidatesConfig(candidates_per_asset=5, version="synthetic_v2"))
    assert set(ids).isdisjoint(c.candidate_id for c in versioned)
    for slot, candidate in enumerate(versioned):
        assert [(p.kind, p.ref) for p in candidate.provenance] == [
            ("stub_asset_generator", f"synthetic_v2:{slot % 5 + 1:02d}")
        ]


def test_identity_is_reproducible_in_separate_interpreters(tmp_path: Path, summary: EpisodeSummary) -> None:
    script = """
import sys
from podcast_pipeline.asset_candidates_stub import DraftCandidatesConfig, generate_draft_candidates
from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
assets = generate_draft_candidates(
    episode_summary=EpisodeSummary.model_validate_json(sys.stdin.read()),
    chapters=['00:00 Synthetic chapter'], config=DraftCandidatesConfig(),
)
import json
print(json.dumps({key: [c.model_dump(mode='json') for c in values] for key, values in assets.items()}, sort_keys=True))
"""
    results = []
    for seed in ("1", "987654"):
        workspace = tmp_path / seed
        workspace.mkdir()
        result = subprocess.run(
            [sys.executable, "-c", script],
            input=summary.model_dump_json(),
            text=True,
            capture_output=True,
            check=True,
            cwd=workspace,
            env={**os.environ, "PYTHONHASHSEED": seed},
            timeout=30,
        )
        results.append(json.loads(result.stdout))
    assert results[0] == results[1]
    local = generate_draft_candidates(
        episode_summary=summary, chapters=["00:00 Synthetic chapter"], config=DraftCandidatesConfig()
    )
    assert results[0] == {key: [c.model_dump(mode="json") for c in values] for key, values in local.items()}


@pytest.mark.parametrize("count", [0, -1])
def test_invalid_candidate_counts_are_rejected(count: int, summary: EpisodeSummary) -> None:
    with pytest.raises(ValueError, match="candidates_per_asset must be >= 1"):
        generate_draft_candidates(
            episode_summary=summary, chapters=[], config=DraftCandidatesConfig(candidates_per_asset=count)
        )
