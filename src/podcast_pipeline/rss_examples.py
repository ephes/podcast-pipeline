from __future__ import annotations

import asyncio
import html
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any
from xml.etree import ElementTree

import httpx

from podcast_pipeline.workspace_store import atomic_write_text


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
    keywords: tuple[str, ...] = ()

    def example_id(self) -> str:
        seed = json.dumps([safe_rss_source(self.feed_url), self.guid or self.link or self.title], ensure_ascii=False)
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
            "feed_url": safe_rss_source(self.feed_url),
            "keywords": list(self.keywords),
        }


_INLINE_SPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def safe_rss_source(value: str) -> str:
    """Canonical provenance; the original URL belongs only to the transport."""
    try:
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError
        return str(url.copy_with(username="", password="", query=None, fragment=None))
    except (httpx.InvalidURL, ValueError) as exc:
        raise RssExamplesError("Invalid RSS source URL.") from exc


MAX_XML_ELEMENTS = 100_000
MAX_XML_DEPTH = 64
MAX_XML_TEXT_BYTES = 4 * 1024 * 1024


class _ExampleTarget:
    """Count all XML work, retaining only one requested item subtree at a time."""

    def __init__(self, *, feed_url: str, limit: int) -> None:
        self.feed_url = feed_url
        self.limit = limit
        self.stack: list[str] = []
        self.elements = 0
        self.text_bytes = 0
        self.channel_depth: int | None = None
        self.found_channel = False
        self.builder: ElementTree.TreeBuilder | None = None
        self.examples: list[RssEpisodeExample] = []

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        self.elements += 1
        self.stack.append(_strip_namespace(tag))
        if self.elements > MAX_XML_ELEMENTS or len(self.stack) > MAX_XML_DEPTH:
            raise RssExamplesError("RSS XML element/depth limit exceeded.")
        self._charge_text(sum(len(k.encode()) + len(v.encode()) for k, v in attrs.items()))
        if self.stack[-1] == "channel" and len(self.stack) <= 2 and not self.found_channel:
            self.channel_depth = len(self.stack)
            self.found_channel = True
        if (
            self.channel_depth is not None
            and len(self.stack) == self.channel_depth + 1
            and self.stack[-1] == "item"
            and len(self.examples) < self.limit
        ):
            self.builder = ElementTree.TreeBuilder()
        if self.builder is not None:
            self.builder.start(tag, attrs)

    def end(self, tag: str) -> None:
        if self.builder is not None:
            self.builder.end(tag)
            if self.channel_depth is not None and len(self.stack) == self.channel_depth + 1:
                item = self.builder.close()
                self.builder = None
                example = _parse_item(item, feed_url=self.feed_url)
                if example is not None:
                    self.examples.append(example)
        if len(self.stack) == self.channel_depth:
            self.channel_depth = None
        self.stack.pop()

    def _charge_text(self, size: int) -> None:
        self.text_bytes += size
        if self.text_bytes > MAX_XML_TEXT_BYTES:
            raise RssExamplesError("RSS XML text limit exceeded.")

    def data(self, data: str) -> None:
        self._charge_text(len(data.encode("utf-8")))
        if self.builder is not None:
            self.builder.data(data)

    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise RssExamplesError("RSS DTD/entity declarations are unsupported.")

    def close(self) -> list[RssEpisodeExample]:
        if not self.found_channel:
            raise RssExamplesError("RSS feed missing channel element (expected RSS 2.0).")
        if not self.examples:
            raise RssExamplesError("RSS feed returned no usable episodes.")
        return self.examples


def fetch_rss_examples(
    *,
    feed_url: str,
    limit: int,
    timeout_seconds: float = 20.0,
) -> list[RssEpisodeExample]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    safe_rss_source(feed_url)
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
    target = _ExampleTarget(feed_url=safe_rss_source(feed_url), limit=limit)
    try:
        if len(xml_payload.encode("utf-8")) > MAX_RSS_BYTES:
            raise RssExamplesError("RSS feed exceeds 8 MiB byte limit")
        parser = ElementTree.XMLParser(target=target)
        for offset in range(0, len(xml_payload), 16 * 1024):
            parser.feed(xml_payload[offset : offset + 16 * 1024])
        parser.close()
        return target.examples
    except (ElementTree.ParseError, UnicodeError) as exc:
        raise RssExamplesError("RSS feed returned invalid XML.") from exc


def write_rss_examples_jsonl(
    *,
    examples: Sequence[RssEpisodeExample],
    output_path: Path,
) -> None:
    output_path = output_path.expanduser()
    content = "".join(
        json.dumps(example.to_record(), ensure_ascii=False, sort_keys=True) + "\n" for example in examples
    )
    atomic_write_text(output_path, content)


MAX_RSS_BYTES = 8 * 1024 * 1024
MAX_RSS_REDIRECTS = 5


def _fetch_rss_xml(*, feed_url: str, timeout_seconds: float) -> str:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise RssExamplesError("RSS timeout must be finite and positive")
    try:
        return asyncio.run(_read_rss_xml(feed_url=feed_url, timeout_seconds=timeout_seconds))
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise RssExamplesError("RSS transfer exceeded its deadline") from exc
    except httpx.RequestError as exc:
        raise RssExamplesError(f"Failed to fetch RSS feed ({type(exc).__name__})") from exc


async def _read_rss_xml(*, feed_url: str, timeout_seconds: float) -> str:
    # Manual streamed redirects avoid reading unbounded intermediate bodies.
    deadline = monotonic() + timeout_seconds
    async with asyncio.timeout(timeout_seconds):
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            request = client.build_request("GET", feed_url, headers={"Accept-Encoding": "identity"})
            for _ in range(MAX_RSS_REDIRECTS + 1):
                response = await client.send(request, stream=True)
                try:
                    if monotonic() >= deadline:
                        raise TimeoutError
                    if response.next_request is not None:
                        request = response.next_request
                        continue
                    return await _read_rss_body(response, deadline=deadline)
                finally:
                    await response.aclose()
    raise RssExamplesError("RSS feed exceeded redirect limit")


async def _read_rss_body(response: httpx.Response, *, deadline: float) -> str:
    if response.status_code >= 400:
        raise RssExamplesError(f"RSS feed request failed with HTTP {response.status_code}.")
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise RssExamplesError("RSS feed returned unsupported Content-Encoding; use an uncompressed feed")
    body = bytearray()
    async for chunk in response.aiter_raw():
        if monotonic() >= deadline:
            raise TimeoutError
        if len(body) + len(chunk) > MAX_RSS_BYTES:
            raise RssExamplesError("RSS feed exceeds 8 MiB byte limit")
        body.extend(chunk)
    if monotonic() >= deadline:
        raise TimeoutError
    # Match HTTPX's text decoding while keeping the allocation bounded.
    return bytes(body).decode(response.encoding or "utf-8", errors="replace")


def _parse_item(item: ElementTree.Element, *, feed_url: str) -> RssEpisodeExample | None:
    title = _extract_first_text(item, ("title",))
    raw_html = _extract_first_html(item, ("encoded", "description"))
    if not title or not raw_html:
        return None

    summary = _extract_first_text(item, ("summary", "subtitle"))
    link = _extract_first_text(item, ("link",))
    guid = _extract_first_text(item, ("guid",))
    published = _extract_first_text(item, ("pubDate", "published", "updated", "date"))
    keywords = _extract_first_text(item, ("keywords",))

    return RssEpisodeExample(
        title=normalize_text(title),
        description_html=normalize_html(raw_html),
        summary=normalize_text(summary) if summary else None,
        link=normalize_text(link) if link else None,
        guid=normalize_text(guid) if guid else None,
        published=normalize_text(published) if published else None,
        feed_url=feed_url,
        keywords=_split_keywords(keywords),
    )


def _split_keywords(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item for item in (normalize_text(part) for part in value.split(",")) if item)


def _extract_first_text(item: ElementTree.Element, tags: Sequence[str]) -> str | None:
    for tag in tags:
        for child in item:
            if _strip_namespace(child.tag) == tag:
                text = "".join(child.itertext()).strip()
                if text:
                    return text
    return None


def _extract_first_html(item: ElementTree.Element, tags: Sequence[str]) -> str | None:
    for tag in tags:
        for child in item:
            if _strip_namespace(child.tag) == tag:
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
    text = _remove_html_comments(text)
    text = _INLINE_SPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _remove_html_comments(text: str) -> str:
    """Scan each comment once; an unterminated opener owns the remaining suffix."""
    parts = []
    pos = 0
    while True:
        start = text.find("<!--", pos)
        if start < 0:
            parts.append(text[pos:])
            break
        parts.append(text[pos:start])
        end = text.find("-->", start + 4)
        if end < 0:
            break
        pos = end + 3
    return "".join(parts)
