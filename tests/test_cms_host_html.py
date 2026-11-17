"""Host links preserve source markup and use only eligible HTML text."""

import pytest

from podcast_pipeline.cms_draft import link_hosts

URL = "https://profiles.example/alice?a=1&b=2"
ANCHOR = '<a href="https://profiles.example/alice?a=1&amp;b=2">Alice</a>'


@pytest.mark.parametrize(
    "prefix",
    [
        '<img alt="> Alice">',
        "<!-- > Alice -->",
        '<script>const text = "> Alice";</script>',
        '<style>p::after { content: "> Alice"; }</style>',
        '<a title=">" href="https://elsewhere.example/">Alice</a>',
        "<template><p>Alice</p></template>",
    ],
)
def test_only_first_visible_host_text_is_linked(prefix: str) -> None:
    source = prefix + "\n<p>Alice and Alice</p>"
    assert link_hosts(source, hosts=["Alice"], host_links={"Alice": URL}) == prefix + f"\n<p>{ANCHOR} and Alice</p>"


@pytest.mark.parametrize("attribute", [f"href='{URL}'", f'HREF = "{URL.replace("&", "&amp;")}"'])
def test_existing_host_anchor_suppresses_another_link(attribute: str) -> None:
    source = f"<p>Alice</p><A {attribute}>profile</A>"
    assert link_hosts(source, hosts=None, host_links={"Alice": URL}) == source


@pytest.mark.parametrize("suffix", ['<img alt="Alice', "<!-- Alice", "<script>Alice", "<a " * 4000])
def test_incomplete_html_is_left_unchanged(suffix: str) -> None:
    source = "<p>Alice</p>" + suffix
    assert link_hosts(source, hosts=None, host_links={"Alice": URL}) == source


def test_escaped_host_and_unrelated_entities_keep_source_spelling() -> None:
    source = "<p>Alice &amp; Bob, then Alice &amp; Bob. &copy; 2026</p>"
    assert link_hosts(source, hosts=None, host_links={"Alice & Bob": URL}) == (
        '<p><a href="https://profiles.example/alice?a=1&amp;b=2">Alice &amp; Bob</a>, '
        "then Alice &amp; Bob. &copy; 2026</p>"
    )
