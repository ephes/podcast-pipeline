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
