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
