from __future__ import annotations

from podcast_pipeline.markdown_html import markdown_to_deterministic_html


def test_markdown_to_deterministic_html_is_stable() -> None:
    markdown = "\n".join(
        [
            "# Title",
            "",
            "See [OpenAI](https://openai.com) and `code`.",
            "",
            "- One",
            "- Two",
            "",
        ],
    )

    expected = "\n".join(
        [
            "<h1>Title</h1>",
            '<p>See <a href="https://openai.com">OpenAI</a> and <code>code</code>.</p>',
            "<ul>",
            "<li>One</li>",
            "<li>Two</li>",
            "</ul>",
            "",
        ],
    )

    rendered = markdown_to_deterministic_html(markdown)
    assert rendered == expected
    assert rendered == markdown_to_deterministic_html(markdown)


def test_markdown_to_deterministic_html_renders_lists_and_paragraphs() -> None:
    markdown = "\n".join(
        [
            "Intro with <tags> & stuff.",
            "",
            "1. First",
            "2. Second with *em* and **strong**",
            "",
            "- Bullet `code`",
            "- Another",
            "",
            "Plain line one",
            "Plain line two",
            "",
        ],
    )

    expected = "\n".join(
        [
            "<p>Intro with &lt;tags&gt; &amp; stuff.</p>",
            "<ol>",
            "<li>First</li>",
            "<li>Second with <em>em</em> and <strong>strong</strong></li>",
            "</ol>",
            "<ul>",
            "<li>Bullet <code>code</code></li>",
            "<li>Another</li>",
            "</ul>",
            "<p>Plain line one Plain line two</p>",
            "",
        ],
    )

    rendered = markdown_to_deterministic_html(markdown)
    assert rendered == expected


def test_markdown_link_rejects_javascript_scheme() -> None:
    md = "[click me](javascript:alert(1))\n"
    rendered = markdown_to_deterministic_html(md)
    assert "javascript:" not in rendered
    assert "click me" in rendered
    assert "<a " not in rendered


def test_markdown_link_allows_https_and_mailto() -> None:
    md = "[site](https://example.com) and [email](mailto:a@b.com)\n"
    rendered = markdown_to_deterministic_html(md)
    assert 'href="https://example.com"' in rendered
    assert 'href="mailto:a@b.com"' in rendered


def test_markdown_link_allows_relative_urls() -> None:
    md = "[doc](./readme.md)\n"
    rendered = markdown_to_deterministic_html(md)
    assert 'href="./readme.md"' in rendered


def test_markdown_link_rejects_data_scheme() -> None:
    md = "[bad](data:text/html,<script>alert(1)</script>)\n"
    rendered = markdown_to_deterministic_html(md)
    assert "data:" not in rendered
    assert "<a " not in rendered
    assert "bad" in rendered


def _inline(markdown: str) -> str:
    rendered = markdown_to_deterministic_html(markdown + "\n")
    assert rendered.startswith("<p>") and rendered.endswith("</p>\n"), rendered
    return rendered[len("<p>") : -len("</p>\n")]


def test_markdown_link_keeps_balanced_parentheses_in_url() -> None:
    md = "- [Python](https://en.wikipedia.org/wiki/Python_(programming_language)) und mehr\n"
    assert markdown_to_deterministic_html(md) == "\n".join(
        [
            "<ul>",
            '<li><a href="https://en.wikipedia.org/wiki/Python_(programming_language)">Python</a> und mehr</li>',
            "</ul>",
            "",
        ],
    )


def test_markdown_link_keeps_nested_parentheses_in_url() -> None:
    assert _inline("See [x](https://example.com/a_(b_(c))_d) now") == (
        'See <a href="https://example.com/a_(b_(c))_d">x</a> now'
    )


def test_markdown_link_ends_at_first_unmatched_close_paren() -> None:
    assert _inline("([x](https://example.com/a) more)") == ('(<a href="https://example.com/a">x</a> more)')


def test_markdown_link_with_unbalanced_open_paren_stays_plain_text() -> None:
    assert _inline("[x](https://example.com/a_(b and more") == ("[x](https://example.com/a_(b and more")


def test_markdown_link_rejects_javascript_scheme_with_balanced_parens() -> None:
    assert _inline("[click me](javascript:alert(1)) after") == "click me after"


def test_markdown_rejected_data_link_leaves_no_stray_paren() -> None:
    assert _inline("[x](data:text/html,<script>alert(1)</script>) after") == "x after"


def test_markdown_asterisks_between_digits_stay_literal() -> None:
    assert _inline("2*3*4 math") == "2*3*4 math"


def test_markdown_asterisks_surrounded_by_spaces_stay_literal() -> None:
    assert _inline("a * b * c") == "a * b * c"


def test_markdown_emphasis_and_strong_still_render() -> None:
    assert _inline("*em* and **strong** and *x **y** z*") == (
        "<em>em</em> and <strong>strong</strong> and <em>x <strong>y</strong> z</em>"
    )


def test_markdown_emphasis_does_not_close_after_whitespace() -> None:
    assert _inline("*a * b*") == "<em>a * b</em>"
    assert _inline("*open but never closed *") == "*open but never closed *"
