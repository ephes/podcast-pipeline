"""Synthetic source boundaries through the generator and production readers."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

from podcast_pipeline.asset_candidates_stub import DraftCandidatesConfig, generate_draft_candidates
from podcast_pipeline.cms_draft import selected_slug
from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.domain.models import AssetKind, TextFormat
from podcast_pipeline.markdown_html import markdown_to_deterministic_html
from podcast_pipeline.tag_parsing import parse_tag_list
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


@pytest.mark.parametrize(
    ("topics", "points", "tag_cycles", "slugs", "subtitles"),
    [
        pytest.param([], [], [["(none)"]], ["podcasting-automation"], ["Podcasting & Automation"], id="empty"),
        pytest.param(
            ["one topic"],
            ["Only takeaway."],
            [["one topic"]],
            ["one-topic-one-topic"],
            ["Only takeaway."],
            id="singleton",
        ),
        pytest.param(
            [" ", "\t", "\n"],
            ["\t", "  "],
            [["(none)"]],
            ["podcasting-automation"],
            ["Podcasting & Automation"],
            id="whitespace-only",
        ),
        pytest.param(
            ["", "  Über Café  ", "\t", "  日本語  "],
            ["", "  Grüße 👋.  ", "\t", " Another insight. "],
            [["Über Café", "日本語"], ["日本語", "Über Café"]],
            ["ber-caf"],
            ["Grüße 👋.", "Another insight."],
            id="padded-unicode-and-blanks",
        ),
        pytest.param(["日本語"], ["Grüße 👋."], [["日本語"]], ["episode"], ["Grüße 👋."], id="non-ascii-slug"),
        pytest.param(
            ["Straße", "strasse", "Django"],
            ["Repeat.", "Repeat."],
            [["Straße", "Django"], ["strasse", "Django"], ["Django", "Straße"]],
            ["stra-e-strasse", "strasse-django", "django-stra-e"],
            ["Repeat."],
            id="repeated-casefold",
        ),
    ],
)
def test_source_boundaries_and_wrapping(
    topics: list[str],
    points: list[str],
    tag_cycles: list[list[str]],
    slugs: list[str],
    subtitles: list[str],
    tmp_path: Path,
) -> None:
    summary = EpisodeSummary(summary_markdown="", topics=topics, key_points=points)
    before = summary.model_dump()
    chapters = [" ", " 00:00 Boundary chapter ", "\t"]
    chapters_before = chapters.copy()
    config = DraftCandidatesConfig(candidates_per_asset=7)
    assets = generate_draft_candidates(episode_summary=summary, chapters=chapters, config=config)
    assert assets == generate_draft_candidates(episode_summary=summary, chapters=chapters, config=config)
    assert summary.model_dump() == before
    assert chapters == chapters_before
    assert set(assets) == {kind.value for kind in AssetKind}
    for candidates in assets.values():
        assert len(candidates) == 7
        for candidate in candidates:
            assert candidate.content.split("\n\n", 1)[1].strip()
            assert all(line.strip() != "-" for line in candidate.content.splitlines())

    slug_path = EpisodeWorkspaceLayout(root=tmp_path).selected_text_path("slug", TextFormat.markdown)
    slug_path.parent.mkdir(parents=True)
    for i in range(7):
        assert parse_tag_list(assets["cms_tags"][i].content) == tag_cycles[i % len(tag_cycles)]
        assert parse_tag_list(assets["audio_tags"][i].content) == tag_cycles[(i + 1) % len(tag_cycles)]
        expected_keywords = (
            ["podcast", "audio", "automation"] if tag_cycles == [["(none)"]] else tag_cycles[i % len(tag_cycles)]
        )
        assert parse_tag_list(assets["itunes_keywords"][i].content) == expected_keywords
        slug_path.write_text(assets["slug"][i].content, encoding="utf-8")
        assert selected_slug(tmp_path) == slugs[i % len(slugs)]
        assert assets["subtitle_auphonic"][i].content.split("\n\n")[1].strip() == subtitles[i % len(subtitles)]
        if tag_cycles == [["(none)"]]:
            assert "A quick tour of the episode." in assets["description"][i].content
            assert "A new episode is out." in assets["summary_short"][i].content
            assert "We cover practical takeaways." in assets["summary_short"][i].content
            assert "#podcast #podcasting" in assets["mastodon"][i].content
        else:
            assert assets["description"][i].content.split("\n\n")[1] == subtitles[i % len(subtitles)]


def test_source_limits_after_rotation() -> None:
    summary = EpisodeSummary(
        summary_markdown="",
        topics=[f"Topic {i:02d}" for i in range(15)],
        key_points=[f"Point {i:02d}." for i in range(8)],
    )
    assets = generate_draft_candidates(
        episode_summary=summary,
        chapters=["00:00 One"],
        config=DraftCandidatesConfig(candidates_per_asset=2),
    )
    assert parse_tag_list(assets["cms_tags"][1].content) == [f"Topic {i:02d}" for i in range(1, 13)]
    assert parse_tag_list(assets["audio_tags"][1].content) == [f"Topic {i:02d}" for i in range(2, 12)]
    assert parse_tag_list(assets["itunes_keywords"][1].content) == [f"Topic {i:02d}" for i in range(1, 9)]
    for asset, limit in [("description", 6), ("shownotes", 3), ("linkedin", 4)]:
        point_lines = [line for line in assets[asset][1].content.splitlines() if line.startswith("- Point")]
        assert point_lines == [f"- Point {i:02d}." for i in range(1, limit + 1)]
    for asset, limit in [("description", 10), ("shownotes", 12), ("youtube_description", 12)]:
        topic_lines = assets[asset][1].content.split("## Topics\n\n")[1].splitlines()
        assert topic_lines == [f"- Topic {i:02d}" for i in range(1, limit + 1)]


class RenderedHTML(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, list[tuple[str, str | None]]]] = []
        self.text: list[str] = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, attrs))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


@pytest.mark.parametrize("asset", ["shownotes", "description", "youtube_description"])
def test_chapter_order_limits_and_safe_html(asset: str) -> None:
    clean = [
        "00:00 CH01 punctuation: (intro)!",
        "00:10 CH02 [docs](https://example.test/a_(b))",
        '00:20 CH03 <script>alert("x")</script>',
        "00:30 CH04 <img src=x onerror=alert(1)> & `code`",
        "00:40 CH05 [unsafe](javascript:alert(1))",
        '00:50 CH06 [quote](https://example.test/?a="x"&b=1)',
        *[f"01:{i:02d} CH{i:02d} chapter" for i in range(7, 16)],
    ]
    chapters = ["", "\t", *[f"  {chapter}  " for chapter in clean], " "]
    original = chapters.copy()
    candidate = generate_draft_candidates(
        episode_summary=EpisodeSummary(summary_markdown="", topics=["Synthetic"], key_points=["Takeaway."]),
        chapters=chapters,
        config=DraftCandidatesConfig(candidates_per_asset=1),
    )[asset][0]
    assert chapters == original
    expected = clean[:12] if asset == "shownotes" else clean
    if asset == "shownotes":
        assert [line[3:] for line in candidate.content.splitlines() if line.startswith("## ")][:-1] == expected
    else:
        assert candidate.content.split("## Chapters\n\n")[1].split("\n\n", 1)[0].splitlines() == [
            f"- {chapter}" for chapter in expected
        ]
    html = markdown_to_deterministic_html(candidate.content)
    parsed = RenderedHTML(html)
    text = "".join(parsed.text)
    positions = [text.index(f"CH{i:02d}") for i in range(1, len(expected) + 1)]
    assert positions == sorted(positions)
    for i in range(len(expected) + 1, 16):
        assert f"CH{i:02d}" not in text
    assert '<script>alert("x")</script>' in text
    assert "<img src=x onerror=alert(1)>" in text
    assert {tag for tag, _ in parsed.elements} <= {"h1", "h2", "p", "ul", "li", "a", "code"}
    links = [attrs for tag, attrs in parsed.elements if tag == "a"]
    assert links == [[("href", "https://example.test/a_(b)")], [("href", 'https://example.test/?a="x"&b=1')]]
    assert all(attrs == [] for tag, attrs in parsed.elements if tag != "a")
    assert "unsafe" in text and "javascript:" not in html
    assert html == markdown_to_deterministic_html(candidate.content)


@pytest.mark.parametrize("asset", ["shownotes", "description", "youtube_description"])
@pytest.mark.parametrize("chapters", [[], [" ", "\t", "\n"]], ids=["empty", "blank"])
def test_missing_chapter_fallbacks(asset: str, chapters: list[str]) -> None:
    candidate = generate_draft_candidates(
        episode_summary=EpisodeSummary(summary_markdown=""),
        chapters=chapters,
        config=DraftCandidatesConfig(candidates_per_asset=1),
    )[asset][0]
    html = markdown_to_deterministic_html(candidate.content)
    if asset == "shownotes":
        assert "<h2>Notes</h2>" in html
        assert "<li>(none)</li>" in html
        assert "## Chapters" not in candidate.content
    else:
        assert "<h2>Chapters</h2>" in html
        assert "<li>(no chapters provided)</li>" in html
