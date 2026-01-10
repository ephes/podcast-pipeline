from __future__ import annotations

import json
from pathlib import Path

import pytest

from podcast_pipeline.rss_examples import (
    RssEpisodeExample,
    RssExamplesError,
    normalize_html,
    parse_rss_examples,
    write_rss_examples_jsonl,
)

_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Episode 1</title>
      <content:encoded><![CDATA[<p>Hello&nbsp;world</p><!--comment-->]]></content:encoded>
      <summary>  Short summary  </summary>
      <link>https://example.com/1</link>
      <guid>guid-1</guid>
      <pubDate>Tue, 01 Jan 2024 00:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Episode 2</title>
      <description>Plain desc</description>
    </item>
    <item>
      <title>Episode 3</title>
    </item>
    <item>
      <description>Missing title</description>
    </item>
  </channel>
</rss>
"""


def test_parse_rss_examples_skips_incomplete_items() -> None:
    examples = parse_rss_examples(_RSS_XML, feed_url="https://example.com/feed", limit=10)

    assert len(examples) == 2
    assert examples[0].title == "Episode 1"
    assert examples[0].description_html == "<p>Hello world</p>"
    assert examples[0].summary == "Short summary"
    assert examples[0].feed_url == "https://example.com/feed"
    assert examples[1].title == "Episode 2"


def test_parse_rss_examples_empty_feed_raises() -> None:
    xml_payload = """<?xml version="1.0"?>
    <rss version="2.0"><channel></channel></rss>
    """
    with pytest.raises(RssExamplesError, match="no usable episodes"):
        parse_rss_examples(xml_payload, feed_url="https://example.com/feed", limit=5)


def test_parse_rss_examples_missing_channel_raises() -> None:
    xml_payload = """<?xml version="1.0"?>
    <rss version="2.0"></rss>
    """
    with pytest.raises(RssExamplesError, match="expected RSS 2.0"):
        parse_rss_examples(xml_payload, feed_url="https://example.com/feed", limit=5)


def test_parse_rss_examples_invalid_xml_raises() -> None:
    with pytest.raises(RssExamplesError, match="invalid XML"):
        parse_rss_examples("<rss", feed_url="https://example.com/feed", limit=5)


def test_normalize_html_collapses_noise() -> None:
    raw = "One&nbsp;two<!--c-->\n\n\nThree"
    assert normalize_html(raw) == "One two\n\nThree"


def test_write_rss_examples_jsonl_writes_records(tmp_path: Path) -> None:
    examples = [
        RssEpisodeExample(
            title="Episode",
            description_html="<p>Desc</p>",
            summary=None,
            link="https://example.com/ep",
            guid="guid-1",
            published=None,
            feed_url="https://example.com/feed",
        )
    ]
    output_path = tmp_path / "rss.jsonl"

    write_rss_examples_jsonl(examples=examples, output_path=output_path)

    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["source"] == "rss"
    assert payload["title"] == "Episode"
