from __future__ import annotations

import json
from pathlib import Path

import pytest

from podcast_pipeline.cms_examples import (
    CmsEpisodeExample,
    CmsExamplesError,
    CmsFieldMapping,
    parse_cms_examples,
    write_cms_examples_jsonl,
)


def test_parse_cms_examples_normalizes_and_falls_back_to_meta() -> None:
    payload = {
        "items": [
            {
                "title": "Episode 1",
                "description": "<p>Hello&nbsp;world</p>",
                "summary": "  Short summary  ",
                "tags": ["AI", "ai", {"name": "Tech"}],
                "url": "https://example.com/1",
                "slug": "ep-1",
                "first_published_at": "2024-01-01",
                "id": 123,
            },
            {
                "title": "Episode 2",
                "shownotes": {"html": "<p>Notes</p>"},
                "meta": {
                    "html_url": "https://example.com/2",
                    "slug": "ep-2",
                    "first_published_at": "2024-02-01",
                },
            },
            {"title": "Episode 3"},
        ]
    }

    examples = parse_cms_examples(payload, api_url="https://cms.example/api", limit=10)

    assert len(examples) == 2

    first = examples[0]
    assert first.title == "Episode 1"
    assert first.description_html == "<p>Hello world</p>"
    assert first.shownotes_html is None
    assert first.tags == ("AI", "Tech")
    assert first.summary == "Short summary"
    assert first.link == "https://example.com/1"
    assert first.slug == "ep-1"
    assert first.published == "2024-01-01"
    assert first.page_id == "123"

    second = examples[1]
    assert second.title == "Episode 2"
    assert second.description_html is None
    assert second.shownotes_html == "<p>Notes</p>"
    assert second.link == "https://example.com/2"
    assert second.slug == "ep-2"
    assert second.published == "2024-02-01"


def test_parse_cms_examples_supports_dot_notation_fields() -> None:
    payload = {
        "items": [
            {
                "title": "Episode Dot",
                "fields": {"description": {"html": "<p>Dot&nbsp;desc</p>"}},
                "attributes": {
                    "summary": "  Dot summary  ",
                    "slug": "dot-slug",
                    "published": "2024-03-01",
                    "id": 987,
                },
                "links": {"public": "https://example.com/primary"},
                "taxonomy": {"tags": ["Tech", "tech", {"name": "Culture"}]},
                "meta": {
                    "html_url": "https://example.com/fallback",
                    "slug": "fallback-slug",
                    "first_published_at": "1999-01-01",
                },
            }
        ]
    }

    fields = CmsFieldMapping(
        title="title",
        summary="attributes.summary",
        description="fields.description.html",
        shownotes=None,
        tags="taxonomy.tags",
        link="links.public",
        slug="attributes.slug",
        published="attributes.published",
        page_id="attributes.id",
    )

    examples = parse_cms_examples(
        payload,
        api_url="https://cms.example/api",
        limit=5,
        fields=fields,
    )

    assert len(examples) == 1
    example = examples[0]
    assert example.description_html == "<p>Dot desc</p>"
    assert example.shownotes_html is None
    assert example.summary == "Dot summary"
    assert example.link == "https://example.com/primary"
    assert example.slug == "dot-slug"
    assert example.published == "2024-03-01"
    assert example.page_id == "987"
    assert example.tags == ("Tech", "Culture")


def test_parse_cms_examples_empty_payload_raises() -> None:
    with pytest.raises(CmsExamplesError, match="no usable episodes"):
        parse_cms_examples({"items": []}, api_url="https://cms.example/api", limit=5)


def test_write_cms_examples_jsonl_writes_records(tmp_path: Path) -> None:
    examples = [
        CmsEpisodeExample(
            title="Episode",
            description_html="<p>Desc</p>",
            shownotes_html=None,
            tags=("Tag",),
            summary=None,
            link="https://example.com/ep",
            slug="ep",
            published=None,
            page_id="42",
            api_url="https://cms.example/api",
        )
    ]
    output_path = tmp_path / "cms.jsonl"

    write_cms_examples_jsonl(examples=examples, output_path=output_path)

    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["source"] == "cms"
    assert payload["title"] == "Episode"
