from __future__ import annotations

import re
from collections.abc import Sequence


def parse_tag_list(text: str | None) -> list[str]:
    if text is None:
        return []

    items = _extract_bullet_items(text)
    if items:
        return normalize_tag_values(items)

    return _parse_non_bulleted_tag_items(_content_lines(text))


def normalize_tag_values(items: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        value = raw.strip()
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _extract_bullet_items(text: str) -> list[str]:
    items: list[str] = []
    for text_line in text.splitlines():
        stripped = text_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not stripped.startswith(("-", "*")):
            continue
        item = stripped.lstrip("-*").strip()
        if item:
            items.append(item)
    return items


def _content_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def _parse_non_bulleted_tag_items(lines: list[str]) -> list[str]:
    if not lines:
        return []

    segments, saw_separator = _split_all_tag_segments(lines)
    if saw_separator:
        return normalize_tag_values(segments)

    if len(lines) == 1:
        words = _split_keyword_blob(lines[0])
        if words:
            return normalize_tag_values(words)

    return normalize_tag_values(lines)


def _split_all_tag_segments(lines: list[str]) -> tuple[list[str], bool]:
    segments: list[str] = []
    saw_separator = False
    for line in lines:
        line_segments, line_had_separator = _split_tag_segments(line)
        segments.extend(line_segments)
        saw_separator = saw_separator or line_had_separator
    return segments, saw_separator


def _split_tag_segments(line: str) -> tuple[list[str], bool]:
    stripped = line.strip()
    if not stripped:
        return [], False
    if any(separator in stripped for separator in (",", ";", "|")):
        return [part.strip() for part in re.split(r"[;,|]", stripped) if part.strip()], True
    return [stripped], False


def _split_keyword_blob(line: str) -> list[str]:
    words = [item for item in re.split(r"\s+", line) if item]
    if len(words) < 8:
        return []
    return words
