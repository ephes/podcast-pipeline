from __future__ import annotations

import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx


class RssExamplesError(RuntimeError):
    pass


@dataclass(frozen=True)
class RssEpisodeExample:
    title: str
    description_html: str
    summary: str | None
    link: str | None
    guid: str | None
    published: str | None
    feed_url: str

    def example_id(self) -> str:
        seed = self.guid or self.link or self.title
        digest = sha256(seed.encode()).hexdigest()[:12]
        return f"rss_{digest}"

    def build_input_text(self) -> str:
        parts = [f"Title: {self.title}"]
        if self.summary:
            parts.append(f"Summary: {self.summary}")
        return "\n".join(parts)

    def to_record(self) -> dict[str, Any]:
        return {
            "version": 1,
            "source": "rss",
            "example_id": self.example_id(),
            "input": self.build_input_text(),
            "output": self.description_html,
            "title": self.title,
            "summary": self.summary,
            "link": self.link,
            "guid": self.guid,
            "published": self.published,
            "feed_url": self.feed_url,
        }


_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_INLINE_SPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def fetch_rss_examples(
    *,
    feed_url: str,
    limit: int,
    timeout_seconds: float = 20.0,
) -> list[RssEpisodeExample]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    xml_payload = _fetch_rss_xml(feed_url=feed_url, timeout_seconds=timeout_seconds)
    return parse_rss_examples(xml_payload, feed_url=feed_url, limit=limit)


def parse_rss_examples(
    xml_payload: str,
    *,
    feed_url: str,
    limit: int,
) -> list[RssEpisodeExample]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    try:
        root = ElementTree.fromstring(xml_payload)
    except ElementTree.ParseError as exc:
        raise RssExamplesError("RSS feed returned invalid XML.") from exc

    channel = _find_channel(root)
    if channel is None:
        raise RssExamplesError("RSS feed missing channel element (expected RSS 2.0).")

    examples: list[RssEpisodeExample] = []
    for item in _iter_items(channel):
        example = _parse_item(item, feed_url=feed_url)
        if example is None:
            continue
        examples.append(example)
        if len(examples) >= limit:
            break

    if not examples:
        raise RssExamplesError("RSS feed returned no usable episodes.")
    return examples


def write_rss_examples_jsonl(
    *,
    examples: Sequence[RssEpisodeExample],
    output_path: Path,
) -> None:
    output_path = output_path.expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for example in examples:
            dumped = json.dumps(example.to_record(), ensure_ascii=False, sort_keys=True)
            handle.write(f"{dumped}\n")


def _fetch_rss_xml(*, feed_url: str, timeout_seconds: float) -> str:
    try:
        response = httpx.get(feed_url, timeout=timeout_seconds, follow_redirects=True)
    except httpx.RequestError as exc:
        raise RssExamplesError(f"Failed to fetch RSS feed: {exc}") from exc
    if response.status_code >= 400:
        raise RssExamplesError(f"RSS feed request failed with HTTP {response.status_code}.")
    return response.text


def _find_channel(root: ElementTree.Element) -> ElementTree.Element | None:
    if _strip_namespace(root.tag) == "channel":
        return root
    for child in root:
        if _strip_namespace(child.tag) == "channel":
            return child
    return None


def _iter_items(channel: ElementTree.Element) -> list[ElementTree.Element]:
    return [child for child in channel if _strip_namespace(child.tag) == "item"]


def _parse_item(item: ElementTree.Element, *, feed_url: str) -> RssEpisodeExample | None:
    title = _extract_first_text(item, ("title",))
    raw_html = _extract_first_html(item, ("encoded", "description"))
    if not title or not raw_html:
        return None

    summary = _extract_first_text(item, ("summary", "subtitle"))
    link = _extract_first_text(item, ("link",))
    guid = _extract_first_text(item, ("guid",))
    published = _extract_first_text(item, ("pubDate", "published", "updated", "date"))

    return RssEpisodeExample(
        title=normalize_text(title),
        description_html=normalize_html(raw_html),
        summary=normalize_text(summary) if summary else None,
        link=normalize_text(link) if link else None,
        guid=normalize_text(guid) if guid else None,
        published=normalize_text(published) if published else None,
        feed_url=feed_url,
    )


def _extract_first_text(item: ElementTree.Element, tags: Sequence[str]) -> str | None:
    for child in item:
        if _strip_namespace(child.tag) in tags:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return None


def _extract_first_html(item: ElementTree.Element, tags: Sequence[str]) -> str | None:
    for child in item:
        if _strip_namespace(child.tag) in tags:
            html_value = _element_inner_xml(child).strip()
            if html_value:
                return html_value
    return None


def _element_inner_xml(element: ElementTree.Element) -> str:
    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in element:
        parts.append(ElementTree.tostring(child, encoding="unicode", method="xml"))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def normalize_text(value: str) -> str:
    text = html.unescape(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ")
    text = _INLINE_SPACE_RE.sub(" ", text)
    return text.strip()


def normalize_html(value: str) -> str:
    text = html.unescape(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ")
    text = _COMMENT_RE.sub("", text)
    text = _INLINE_SPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()
