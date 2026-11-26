"""Host links preserve source markup and use only eligible HTML text."""

import sys

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


def test_host_names_match_named_and_numeric_references_without_rewriting_source() -> None:
    protected = "<a href='https://profiles.example/zoe'>Zo&#235;</a><template>Ren&eacute;e</template>"
    mention = "R&#x65;n&eacute;e"
    source = protected + f"\n<p>Ren&#233;es, {mention} and Zo&#xeb;. &copy; 2026</p>"
    links = {"Renée": URL, "Zoë": "https://profiles.example/zoe"}

    assert link_hosts(source, hosts=None, host_links=links) == (
        protected + f"\n<p>Ren&#233;es, {ANCHOR.replace('Alice', mention)} and Zo&#xeb;. &copy; 2026</p>"
    )


def test_host_boundaries_use_decoded_characters_and_keep_semicolonless_references() -> None:
    source = "<p>&#65;Alice, &ltAlice, Alice&#115;</p>"
    assert link_hosts(source, hosts=None, host_links={"Alice": URL}) == (
        f"<p>&#65;Alice, &lt{ANCHOR}, Alice&#115;</p>"
    )


def test_host_link_never_splits_a_multi_character_reference() -> None:
    source = "<p>Alice &NotEqualTilde; and Alice ≂</p>"
    assert link_hosts(source, hosts=None, host_links={"Alice ≂": URL}) == (
        f"<p>Alice &NotEqualTilde; and {ANCHOR.replace('Alice', 'Alice ≂')}</p>"
    )


def test_long_numeric_references_keep_their_source_spelling() -> None:
    out_of_range = "&#" + "9" * 4500 + ";"
    mention = "&#" + "0" * 4500 + "65;lice"
    assert link_hosts(f"<p>{out_of_range}{mention}</p>", hosts=None, host_links={"Alice": URL}) == (
        f"<p>{out_of_range}{ANCHOR.replace('Alice', mention)}</p>"
    )


@pytest.mark.parametrize(
    "source",
    [
        *(f"<{tag}>&#65;lice</{tag}><p>Alice</p>" for tag in ["xmp", "iframe", "noembed", "noframes", "noscript"]),
        "<plaintext>sample </plaintext><p>Alice</p>",
        "<noscript><noscript></noscript><p>Alice</p>",
        "<p>Alice</p><xmp/>",
    ],
)
def test_raw_text_elements_leave_html_unchanged(source: str) -> None:
    assert link_hosts(source, hosts=None, host_links={"Alice": URL}) == source


@pytest.mark.parametrize("tag", ["script", "style", "a", "template"])
def test_self_closing_nonvoid_tags_are_left_unchanged(tag: str) -> None:
    source = f"<p>Alice</p><{tag}/>Alice</{tag}><p>Alice</p>"
    assert link_hosts(source, hosts=None, host_links={"Alice": URL}) == source


@pytest.mark.parametrize("wrapper", ["{}", "<noscript>{}</noscript>"])
def test_overlong_attribute_reference_leaves_html_unchanged(wrapper: str) -> None:
    source = "<p>Alice</p>" + wrapper.format('<img alt="&#' + "0" * 5000 + '65;">')
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)  # Python's default; PYTHONINTMAXSTRDIGITS may change it.
    try:
        assert link_hosts(source, hosts=None, host_links={"Alice": URL}) == source
    finally:
        sys.set_int_max_str_digits(previous)
