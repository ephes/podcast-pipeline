from __future__ import annotations

import json
from pathlib import Path
from typing import SupportsIndex

import pytest

from podcast_pipeline.few_shot_selector import FewShotExampleRecord
from podcast_pipeline.rss_examples import (
    RssEpisodeExample,
    RssExamplesError,
    _remove_html_comments,
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
    assert payload["keywords"] == []


def test_parse_rss_examples_reads_itunes_keywords() -> None:
    xml_payload = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <item>
      <title>Python 3.13</title>
      <description>Desc</description>
      <itunes:keywords>python,python3.13, pypy ,,jit</itunes:keywords>
    </item>
  </channel>
</rss>
"""
    examples = parse_rss_examples(xml_payload, feed_url="https://example.com/feed", limit=5)

    assert examples[0].keywords == ("python", "python3.13", "pypy", "jit")
    record = FewShotExampleRecord.from_value(examples[0].to_record())
    assert record.keywords == ("python", "python3.13", "pypy", "jit")


@pytest.mark.parametrize("repeat", [1, 2000, 4000, 8000])
def test_unterminated_html_comments_discard_the_remaining_comment(repeat: int) -> None:
    assert normalize_html("<p>Keep</p>" + "<!--" * repeat + "hidden") == "<p>Keep</p>"


def test_multiple_html_comments_preserve_surrounding_text() -> None:
    assert normalize_html("before<!--one-->between<!--two-->after") == "beforebetweenafter"


class _CountedSearchText(str):
    work: int = 0

    def find(self, sub: str, start: SupportsIndex | None = 0, end: SupportsIndex | None = None) -> int:
        begin, stop, _ = slice(start, end).indices(len(self))
        result = super().find(sub, start, end)
        self.work += (result + len(sub) if result >= 0 else stop) - begin
        return result


@pytest.mark.parametrize("count", [1000, 2000, 4000, 8000])
@pytest.mark.parametrize("unit", ["<!--", "<!--ok-->"])
def test_comment_search_work_is_linear(count: int, unit: str) -> None:
    text = _CountedSearchText("before" + unit * count + "after")
    _remove_html_comments(text)
    assert text.work <= 2 * len(text)


def test_comment_scanner_preserves_escaped_delimiters() -> None:
    text = "&lt;!-- literal --&gt;<!--real comment-->text"
    assert _remove_html_comments(text) == "&lt;!-- literal --&gt;text"


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("before<p>inside</p>after", "before<p>inside</p>after"),
        ("start<b>bold</b>middle<i>italic</i>end", "start<b>bold</b>middle<i>italic</i>end"),
        ("plain text", "plain text"),
        ("<![CDATA[<p>CDATA</p>tail]]>", "<p>CDATA</p>tail"),
    ],
)
def test_rss_mixed_content_tails_appear_once(description: str, expected: str) -> None:
    xml = f"<rss><channel><item><title>Episode</title><description>{description}</description></item></channel></rss>"
    example = parse_rss_examples(xml, feed_url="https://feed.example/", limit=1)[0]
    assert example.description_html == expected


def test_rss_namespaced_child_tail_appears_once() -> None:
    from xml.etree import ElementTree

    xml = """<rss xmlns:x="urn:example"><channel><item><title>Episode</title>
    <description>before<x:p>inside</x:p>after</description></item></channel></rss>"""
    example = parse_rss_examples(xml, feed_url="https://feed.example/", limit=1)[0]
    result = ElementTree.fromstring("<description>" + example.description_html + "</description>")
    assert result.text == "before"
    assert result[0].tag == "{urn:example}p"
    assert result[0].text == "inside"
    assert result[0].tail == "after"


@pytest.mark.parametrize("reverse", [False, True])
def test_rss_declared_content_preferences_override_document_order(reverse: bool) -> None:
    fields = [
        "<description>Short description</description>",
        "<content:encoded><![CDATA[<p>Full content</p>]]></content:encoded>",
        "<itunes:subtitle>Subtitle</itunes:subtitle>",
        "<summary>Preferred summary</summary>",
        "<date>Other date</date>",
        "<updated>Updated</updated>",
        "<published>Published</published>",
        "<pubDate>Preferred publication</pubDate>",
    ]
    if reverse:
        fields.reverse()
    xml = (
        '<rss xmlns:content="urn:content" xmlns:itunes="urn:itunes"><channel><item><title>Episode</title>'
        + "".join(fields)
        + "</item></channel></rss>"
    )
    example = parse_rss_examples(xml, feed_url="https://feed.example/", limit=1)[0]
    assert example.description_html == "<p>Full content</p>"
    assert example.summary == "Preferred summary"
    assert example.published == "Preferred publication"


@pytest.mark.parametrize(
    ("fields", "description", "summary"),
    [
        (
            "<description>Fallback</description><encoded> </encoded><summary> </summary><subtitle>Sub</subtitle>",
            "Fallback",
            "Sub",
        ),
        (
            "<description>Fallback</description><encoded/><encoded>First</encoded><encoded>Second</encoded>"
            "<subtitle>Sub</subtitle><summary/><summary>First summary</summary><summary>Second summary</summary>",
            "First",
            "First summary",
        ),
    ],
)
def test_rss_preferences_fall_back_and_keep_same_tag_order(fields: str, description: str, summary: str) -> None:
    xml = "<rss><channel><item><title>Episode</title>" + fields + "</item></channel></rss>"
    example = parse_rss_examples(xml, feed_url="https://feed.example/", limit=1)[0]
    assert example.description_html == description
    assert example.summary == summary
