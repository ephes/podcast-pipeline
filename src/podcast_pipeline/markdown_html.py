from __future__ import annotations

import html
import re

_HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<text>.+?)\s*$")
_UL_ITEM_RE = re.compile(r"^(?P<bullet>[-*+])\s+(?P<text>.+?)\s*$")
_OL_ITEM_RE = re.compile(r"^(?P<num>\d+)\.\s+(?P<text>.+?)\s*$")


def markdown_to_deterministic_html(markdown_text: str) -> str:
    """Render a small, deterministic Markdown subset to HTML for RichText copy/paste.

    Supported:
    - Headings: `#` .. `######`
    - Unordered lists: `- `, `* `, `+ `
    - Ordered lists: `1. `
    - Paragraphs (consecutive non-empty lines are joined with spaces)
    - Inline links: `[label](url)`; parentheses in the URL must be balanced
    - Inline code: `` `code` ``
    - Inline emphasis: `*em*` and `**strong**` (best-effort); a single `*` opens
      emphasis only before non-whitespace and not between two letters or digits,
      and closes it only after non-whitespace, so `2*3*4` and `a * b * c` stay
      literal (intraword `foo*bar*` emphasis is not supported)
    """
    renderer = _BlockRenderer()
    for raw in markdown_text.splitlines():
        _consume_markdown_line(renderer, raw)

    renderer.flush_paragraph()
    renderer.close_list()
    return "\n".join(renderer.out).rstrip() + "\n"


class _BlockRenderer:
    def __init__(self) -> None:
        self.out: list[str] = []
        self.paragraph_lines: list[str] = []
        self.list_kind: str | None = None  # "ul" | "ol"

    def flush_paragraph(self) -> None:
        if not self.paragraph_lines:
            return
        text = " ".join(line.strip() for line in self.paragraph_lines if line.strip())
        if text:
            self.out.append(f"<p>{_render_inline(text)}</p>")
        self.paragraph_lines = []

    def close_list(self) -> None:
        if self.list_kind is None:
            return
        self.out.append(f"</{self.list_kind}>")
        self.list_kind = None

    def ensure_list(self, kind: str) -> None:
        if self.list_kind == kind:
            return
        self.close_list()
        self.out.append(f"<{kind}>")
        self.list_kind = kind

    def add_heading(self, level: int, text: str) -> None:
        self.out.append(f"<h{level}>{_render_inline(text)}</h{level}>")

    def add_list_item(self, text: str) -> None:
        self.out.append(f"<li>{_render_inline(text)}</li>")

    def add_paragraph_line(self, line: str) -> None:
        self.paragraph_lines.append(line)


def _consume_markdown_line(renderer: _BlockRenderer, raw: str) -> None:
    line = raw.rstrip()
    if not line.strip():
        renderer.flush_paragraph()
        renderer.close_list()
        return

    if (match := _HEADING_RE.match(line)) is not None:
        renderer.flush_paragraph()
        renderer.close_list()
        renderer.add_heading(len(match.group("level")), match.group("text").strip())
        return

    if (match := _UL_ITEM_RE.match(line)) is not None:
        renderer.flush_paragraph()
        renderer.ensure_list("ul")
        renderer.add_list_item(match.group("text").strip())
        return

    if (match := _OL_ITEM_RE.match(line)) is not None:
        renderer.flush_paragraph()
        renderer.ensure_list("ol")
        renderer.add_list_item(match.group("text").strip())
        return

    renderer.close_list()
    renderer.add_paragraph_line(line)


def _render_inline(text: str) -> str:
    out: list[str] = []
    idx = 0
    while idx < len(text):
        rendered = _try_render_inline_token(text, idx)
        if rendered is None:
            out.append(html.escape(text[idx]))
            idx += 1
            continue
        rendered_html, next_idx = rendered
        out.append(rendered_html)
        idx = next_idx

    return "".join(out)


def _try_render_inline_token(text: str, idx: int) -> tuple[str, int] | None:
    for parser in (
        _try_render_code,
        _try_render_link,
        _try_render_strong,
        _try_render_em,
    ):
        rendered = parser(text, idx)
        if rendered is not None:
            return rendered
    return None


def _try_render_code(text: str, idx: int) -> tuple[str, int] | None:
    if not text.startswith("`", idx):
        return None
    end = text.find("`", idx + 1)
    if end == -1:
        return None
    code = text[idx + 1 : end]
    return f"<code>{html.escape(code)}</code>", end + 1


_ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto", ""})


def _is_safe_url(url: str) -> bool:
    """Check if a URL uses a safe scheme (http, https, mailto, or relative)."""
    colon = url.find(":")
    if colon < 0:
        return True  # relative URL, no scheme
    scheme = url[:colon].lower().strip()
    return scheme in _ALLOWED_URL_SCHEMES


def _try_render_link(text: str, idx: int) -> tuple[str, int] | None:
    if not text.startswith("[", idx):
        return None
    close = text.find("]", idx + 1)
    if close == -1 or close + 1 >= len(text) or text[close + 1] != "(":
        return None
    end = _find_link_destination_end(text, close + 2)
    if end is None:
        return None

    label = text[idx + 1 : close]
    url = text[close + 2 : end].strip()
    if not _is_safe_url(url):
        # Render label text only, skip the dangerous link
        return _render_inline(label), end + 1
    label_html = _render_inline(label)
    href = html.escape(url, quote=True)
    return f'<a href="{href}">{label_html}</a>', end + 1


def _find_link_destination_end(text: str, start: int) -> int | None:
    """Return the index of the `)` that closes a link destination starting at `start`.

    Parentheses inside the destination must be balanced, as in CommonMark, so URLs
    such as `https://en.wikipedia.org/wiki/Python_(programming_language)` stay whole.
    Returns None when the destination is never closed.
    """
    depth = 0
    for pos in range(start, len(text)):
        char = text[pos]
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return pos
            depth -= 1
    return None


def _can_open_emphasis(text: str, idx: int) -> bool:
    """A `*` opens emphasis only when followed by non-whitespace and not flanked by
    alphanumerics on both sides (simplified CommonMark flanking rule)."""
    after = idx + 1
    if after >= len(text) or text[after].isspace():
        return False
    return not (idx > 0 and text[idx - 1].isalnum() and text[after].isalnum())


def _try_render_strong(text: str, idx: int) -> tuple[str, int] | None:
    if not text.startswith("**", idx):
        return None
    end = text.find("**", idx + 2)
    if end == -1:
        return None
    inner = text[idx + 2 : end]
    return f"<strong>{_render_inline(inner)}</strong>", end + 2


def _try_render_em(text: str, idx: int) -> tuple[str, int] | None:
    if not text.startswith("*", idx) or not _can_open_emphasis(text, idx):
        return None
    pos = idx + 2
    while pos < len(text):
        if text.startswith("**", pos):
            # A `**` run belongs to nested strong emphasis, not to this `*`.
            pos += 2
            continue
        if text[pos] == "*" and not text[pos - 1].isspace():
            inner = text[idx + 1 : pos]
            return f"<em>{_render_inline(inner)}</em>", pos + 1
        pos += 1
    return None
