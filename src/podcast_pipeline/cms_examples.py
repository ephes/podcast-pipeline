from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from podcast_pipeline.rss_examples import normalize_html, normalize_text


class CmsExamplesError(RuntimeError):
    pass


@dataclass(frozen=True)
class CmsFieldMapping:
    title: str = "title"
    summary: str | None = "summary"
    description: str | None = "description"
    shownotes: str | None = "shownotes"
    tags: str | None = "tags"
    link: str | None = "url"
    slug: str | None = "slug"
    published: str | None = "first_published_at"
    page_id: str | None = "id"


@dataclass(frozen=True)
class CmsEpisodeExample:
    title: str
    description_html: str | None
    shownotes_html: str | None
    tags: tuple[str, ...]
    summary: str | None
    link: str | None
    slug: str | None
    published: str | None
    page_id: str | None
    api_url: str

    def example_id(self) -> str:
        seed = self.page_id or self.link or self.title
        digest = sha256(seed.encode()).hexdigest()[:12]
        return f"cms_{digest}"

    def build_input_text(self) -> str:
        parts = [f"Title: {self.title}"]
        if self.summary:
            parts.append(f"Summary: {self.summary}")
        return "\n".join(parts)

    def build_output_text(self) -> str:
        if self.description_html:
            return self.description_html
        if self.shownotes_html:
            return self.shownotes_html
        return ""

    def to_record(self) -> dict[str, Any]:
        return {
            "version": 1,
            "source": "cms",
            "example_id": self.example_id(),
            "input": self.build_input_text(),
            "output": self.build_output_text(),
            "title": self.title,
            "summary": self.summary,
            "link": self.link,
            "slug": self.slug,
            "published": self.published,
            "tags": list(self.tags),
            "description_html": self.description_html,
            "shownotes_html": self.shownotes_html,
            "api_url": self.api_url,
            "page_id": self.page_id,
        }


_TAG_SPLIT_RE = re.compile(r"[;,]")


def fetch_cms_examples(
    *,
    api_url: str,
    limit: int,
    timeout_seconds: float = 20.0,
    fields: CmsFieldMapping | None = None,
) -> list[CmsEpisodeExample]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    payload = _fetch_cms_json(api_url=api_url, timeout_seconds=timeout_seconds, limit=limit)
    return parse_cms_examples(payload, api_url=api_url, limit=limit, fields=fields)


def parse_cms_examples(
    payload: Any,
    *,
    api_url: str,
    limit: int,
    fields: CmsFieldMapping | None = None,
) -> list[CmsEpisodeExample]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    mapping = fields or CmsFieldMapping()
    items = _extract_items(payload)

    examples: list[CmsEpisodeExample] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        example = _parse_item(item, api_url=api_url, fields=mapping)
        if example is None:
            continue
        if not example.build_output_text():
            continue
        examples.append(example)
        if len(examples) >= limit:
            break

    if not examples:
        raise CmsExamplesError("CMS API returned no usable episodes.")
    return examples


def write_cms_examples_jsonl(
    *,
    examples: Sequence[CmsEpisodeExample],
    output_path: Path,
) -> None:
    output_path = output_path.expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for example in examples:
            dumped = json.dumps(example.to_record(), ensure_ascii=False, sort_keys=True)
            handle.write(f"{dumped}\n")


def _fetch_cms_json(*, api_url: str, timeout_seconds: float, limit: int) -> Any:
    params = {"limit": limit}
    try:
        response = httpx.get(api_url, timeout=timeout_seconds, follow_redirects=True, params=params)
    except httpx.RequestError as exc:
        raise CmsExamplesError(f"Failed to fetch CMS API: {exc}") from exc
    if response.status_code >= 400:
        raise CmsExamplesError(f"CMS API request failed with HTTP {response.status_code}.")
    try:
        return response.json()
    except ValueError as exc:
        raise CmsExamplesError("CMS API returned invalid JSON.") from exc


def _extract_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("items", "results", "pages"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise CmsExamplesError("CMS API response missing items list.")


def _parse_item(
    item: Mapping[str, Any],
    *,
    api_url: str,
    fields: CmsFieldMapping,
) -> CmsEpisodeExample | None:
    title = _coerce_text(_extract_field(item, fields.title))
    if not title:
        return None

    description = _coerce_html(_extract_field(item, fields.description))
    shownotes = _coerce_html(_extract_field(item, fields.shownotes))
    tags = _coerce_tags(_extract_field(item, fields.tags))
    summary = _coerce_text(_extract_field(item, fields.summary))
    link = _coerce_text(_extract_field(item, fields.link))
    if link is None:
        link = _coerce_text(
            _extract_meta(item, "html_url") or _extract_meta(item, "url") or _extract_meta(item, "detail_url")
        )
    slug = _coerce_text(_extract_field(item, fields.slug))
    if slug is None:
        slug = _coerce_text(_extract_meta(item, "slug"))
    published = _coerce_text(_extract_field(item, fields.published))
    if published is None:
        published = _coerce_text(_extract_meta(item, "first_published_at") or _extract_meta(item, "published_at"))
    page_id = _coerce_id(_extract_field(item, fields.page_id))

    if not description and not shownotes:
        return None

    return CmsEpisodeExample(
        title=title,
        description_html=description,
        shownotes_html=shownotes,
        tags=tags,
        summary=summary,
        link=link,
        slug=slug,
        published=published,
        page_id=page_id,
        api_url=api_url,
    )


def _extract_field(item: Mapping[str, Any], field: str | None) -> Any:
    if not field:
        return None
    current: Any = item
    for part in field.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _extract_meta(item: Mapping[str, Any], field: str) -> Any:
    meta = item.get("meta")
    if not isinstance(meta, Mapping):
        return None
    return meta.get(field)


def _coerce_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = normalize_text(value)
        return text or None
    if isinstance(value, Mapping):
        nested = value.get("value") or value.get("text") or value.get("html")
        return _coerce_text(nested)
    return normalize_text(str(value)) or None


def _coerce_html(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        html_value = normalize_html(value)
        return html_value or None
    if isinstance(value, Mapping):
        nested = value.get("html") or value.get("value")
        return _coerce_html(nested)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        parts = [_coerce_html(item) for item in value]
        combined = "\n\n".join(part for part in parts if part)
        return normalize_html(combined) if combined else None
    return normalize_html(str(value)) or None


def _coerce_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip() or None


def _coerce_tags(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = _TAG_SPLIT_RE.split(value)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = [_coerce_tag(item) for item in value]
    elif isinstance(value, Mapping):
        items = [_coerce_tag(value)]
    else:
        items = [str(value)]

    cleaned = []
    for item in items:
        if not item:
            continue
        text = normalize_text(item)
        if text:
            cleaned.append(text)

    seen: set[str] = set()
    deduped: list[str] = []
    for item in cleaned:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return tuple(deduped)


def _coerce_tag(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("name", "title", "slug"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested
    return str(value)
