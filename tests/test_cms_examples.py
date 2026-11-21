from __future__ import annotations

import json
from pathlib import Path

import pytest

from podcast_pipeline.cms_examples import (
    CmsEpisodeExample,
    CmsExamplesError,
    CmsFieldMapping,
    parse_cms_examples,
    write_cms_examples_jsonl,
)


def test_parse_cms_examples_normalizes_and_falls_back_to_meta() -> None:
    payload = {
        "items": [
            {
                "title": "Episode 1",
                "description": "<p>Hello&nbsp;world</p>",
                "summary": "  Short summary  ",
                "tags": ["AI", "ai", {"name": "Tech"}],
                "url": "https://example.com/1",
                "slug": "ep-1",
                "first_published_at": "2024-01-01",
                "id": 123,
            },
            {
                "title": "Episode 2",
                "shownotes": {"html": "<p>Notes</p>"},
                "meta": {
                    "html_url": "https://example.com/2",
                    "slug": "ep-2",
                    "first_published_at": "2024-02-01",
                },
            },
            {"title": "Episode 3"},
        ]
    }

    examples = parse_cms_examples(payload, api_url="https://cms.example/api", limit=10)

    assert len(examples) == 2

    first = examples[0]
    assert first.title == "Episode 1"
    assert first.description_html == "<p>Hello&nbsp;world</p>"
    assert first.shownotes_html is None
    assert first.tags == ("AI", "Tech")
    assert first.summary == "Short summary"
    assert first.link == "https://example.com/1"
    assert first.slug == "ep-1"
    assert first.published == "2024-01-01"
    assert first.page_id == "123"

    second = examples[1]
    assert second.title == "Episode 2"
    assert second.description_html is None
    assert second.shownotes_html == "<p>Notes</p>"
    assert second.link == "https://example.com/2"
    assert second.slug == "ep-2"
    assert second.published == "2024-02-01"


def test_parse_cms_examples_supports_dot_notation_fields() -> None:
    payload = {
        "items": [
            {
                "title": "Episode Dot",
                "fields": {"description": {"html": "<p>Dot&nbsp;desc</p>"}},
                "attributes": {
                    "summary": "  Dot summary  ",
                    "slug": "dot-slug",
                    "published": "2024-03-01",
                    "id": 987,
                },
                "links": {"public": "https://example.com/primary"},
                "taxonomy": {"tags": ["Tech", "tech", {"name": "Culture"}]},
                "meta": {
                    "html_url": "https://example.com/fallback",
                    "slug": "fallback-slug",
                    "first_published_at": "1999-01-01",
                },
            }
        ]
    }

    fields = CmsFieldMapping(
        title="title",
        summary="attributes.summary",
        description="fields.description.html",
        shownotes=None,
        tags="taxonomy.tags",
        link="links.public",
        slug="attributes.slug",
        published="attributes.published",
        page_id="attributes.id",
    )

    examples = parse_cms_examples(
        payload,
        api_url="https://cms.example/api",
        limit=5,
        fields=fields,
    )

    assert len(examples) == 1
    example = examples[0]
    assert example.description_html == "<p>Dot&nbsp;desc</p>"
    assert example.shownotes_html is None
    assert example.summary == "Dot summary"
    assert example.link == "https://example.com/primary"
    assert example.slug == "dot-slug"
    assert example.published == "2024-03-01"
    assert example.page_id == "987"
    assert example.tags == ("Tech", "Culture")


def test_parse_cms_examples_empty_payload_raises() -> None:
    with pytest.raises(CmsExamplesError, match="no usable episodes"):
        parse_cms_examples({"items": []}, api_url="https://cms.example/api", limit=5)


def test_write_cms_examples_jsonl_writes_records(tmp_path: Path) -> None:
    examples = [
        CmsEpisodeExample(
            title="Episode",
            description_html="<p>Desc</p>",
            shownotes_html=None,
            tags=("Tag",),
            summary=None,
            link="https://example.com/ep",
            slug="ep",
            published=None,
            page_id="42",
            api_url="https://cms.example/api",
        )
    ]
    output_path = tmp_path / "cms.jsonl"

    write_cms_examples_jsonl(examples=examples, output_path=output_path)

    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["source"] == "cms"
    assert payload["title"] == "Episode"


def test_cms_source_identity_preserves_distinct_sites_in_selection() -> None:
    from podcast_pipeline.few_shot_selector import select_few_shot_examples

    payload = [{"id": 42, "title": "Episode", "description": "<p>Notes</p>"}]
    first = parse_cms_examples(payload, api_url="https://first.example/api", limit=1)[0]
    second = parse_cms_examples(payload, api_url="https://second.example/api", limit=1)[0]
    assert first.example_id() != second.example_id()
    assert len(select_few_shot_examples(examples=[first.to_record(), second.to_record()], topics=[], limit=2)) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://CMS.example:443/api/?limit=5#fragment",
        "https://cms.example/api?limit=25",
        "https://user:pass@cms.example/api",
    ],
)
def test_cms_source_identity_is_stable_within_source(url: str) -> None:
    payload = [{"id": 42, "title": "Episode", "description": "<p>Notes</p>"}]
    first = parse_cms_examples(payload, api_url="https://cms.example/api", limit=1)[0]
    repeat = parse_cms_examples(payload, api_url=url, limit=1)[0]
    assert first.example_id() == repeat.example_id()


@pytest.mark.parametrize(
    ("path", "other"), [("a%3Fb", "a"), ("a%23b", "a"), ("a%0Ab", "ab"), ("a%2Fb", "a/b"), ("a%2F", "a/")]
)
def test_cms_source_preserves_encoded_paths(path: str, other: str) -> None:
    payload = [{"id": 42, "title": "Episode", "description": "<p>Notes</p>"}]
    encoded = parse_cms_examples(payload, api_url=f"https://cms.example/{path}", limit=1)[0]
    decoded = parse_cms_examples(payload, api_url=f"https://cms.example/{other}", limit=1)[0]
    assert encoded.example_id() != decoded.example_id()
    assert encoded.to_record()["example_id"] == encoded.example_id()


def test_cms_identity_distinguishes_api_paths() -> None:
    payload = [{"id": 42, "title": "Episode", "description": "<p>Notes</p>"}]
    first = parse_cms_examples(payload, api_url="https://cms.example/api", limit=1)[0]
    second = parse_cms_examples(payload, api_url="https://cms.example/other", limit=1)[0]
    assert first.example_id() != second.example_id()


@pytest.mark.parametrize("page_id", [{"value": 42}, [42], True, False, 42.0, " \t"])
@pytest.mark.parametrize("link", ["https://cms.example/episode", None])
def test_cms_unsupported_page_id_uses_same_identity_as_missing_id(page_id: object, link: str | None) -> None:
    item = {"title": "Episode", "description": "<p>Notes</p>", "url": link}
    with_id, without_id = parse_cms_examples(
        [{**item, "id": page_id}, item],
        api_url="https://cms.example/api",
        limit=2,
    )

    assert with_id.page_id is None
    assert with_id.to_record() == without_id.to_record()


@pytest.mark.parametrize("page_id", [42, " 42 "])
def test_cms_id_digest_is_stable(page_id: int | str) -> None:
    example = parse_cms_examples(
        [{"id": page_id, "title": "Episode", "description": "<p>Notes</p>"}],
        api_url="https://cms.example/api",
        limit=1,
    )[0]
    assert example.to_record()["page_id"] == "42"
    assert example.example_id() == "cms_fc79d2e15716"


def test_export_strips_url_credentials_from_records_selection_and_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from podcast_pipeline import cms_examples
    from podcast_pipeline.entrypoints.cli import app
    from podcast_pipeline.few_shot_selector import select_few_shot_examples

    api_url = "https://synthetic-user:synthetic-password@cms.example/api/?token=synthetic-token#synthetic-fragment"
    public_url = "https://link-user:link-password@example.com/episode?key=link-token#link-fragment"
    calls: list[str] = []

    def read(*, api_url: str, timeout_seconds: float, limit: int) -> bytes:
        calls.append(api_url)
        return json.dumps(
            {"items": [{"id": 42, "title": "Episode", "description": "<p>Notes</p>", "url": public_url}]}
        ).encode()

    monkeypatch.setattr(cms_examples, "_read_cms_response", read)
    output = tmp_path / "examples.jsonl"
    result = CliRunner().invoke(app, ["cms-examples", "--api-url", api_url, "--output", str(output)])
    assert result.exit_code == 0
    assert calls == [api_url]
    record = json.loads(output.read_text())
    selected = select_few_shot_examples(examples=[record], topics=[], limit=1)
    for text in (output.read_text(), repr(selected), result.output):
        for secret in (
            "synthetic-user",
            "synthetic-password",
            "synthetic-token",
            "synthetic-fragment",
            "link-user",
            "link-password",
            "link-token",
            "link-fragment",
        ):
            assert secret not in text
    assert record["api_url"] == "https://cms.example/api"
    assert record["link"] == "https://example.com/episode"
    assert record["example_id"] == "cms_fc79d2e15716"


@pytest.mark.parametrize(
    "body",
    [
        b'{"items":[{"title":"private-first","title":"private-second","description":"ok"}]}',
        b'{"items":[{"id":1,"id":2,"title":"Episode","description":"ok"}]}',
        b'{"items":[{"title":"Episode","description":{"html":"first","html":"second"}}]}',
        b'{"items":[],"items":[{"title":"Episode","description":"ok"}]}',
        b"[" * 2000 + b"0" + b"]" * 2000,
        b"[" * 65 + b"0" + b"]" * 65,
    ],
)
def test_ambiguous_or_deep_cms_json_preserves_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    from typer.testing import CliRunner

    from podcast_pipeline import cms_examples
    from podcast_pipeline.entrypoints.cli import app

    monkeypatch.setattr(cms_examples, "_read_cms_response", lambda **kwargs: body)
    output = tmp_path / "examples.jsonl"
    output.write_bytes(b"previous corpus\n")
    result = CliRunner().invoke(app, ["cms-examples", "--api-url", "https://cms.example/api", "--output", str(output)])
    assert result.exit_code == 2
    assert "CMS API returned invalid JSON" in result.output
    assert "private-first" not in result.output and "private-second" not in result.output
    assert output.read_bytes() == b"previous corpus\n"


def test_query_identified_links_stay_distinct_and_usable() -> None:
    examples = parse_cms_examples(
        [
            {"title": "First", "description": "<p>a</p>", "url": "https://blog.example/?p=123&access_token=t#x"},
            {"title": "Second", "description": "<p>b</p>", "url": "https://blog.example/?p=124"},
        ],
        api_url="https://blog.example/wp-json",
        limit=2,
    )
    assert [example.link for example in examples] == ["https://blog.example/?p=123", "https://blog.example/?p=124"]
    assert examples[0].example_id() != examples[1].example_id()


@pytest.mark.parametrize(
    ("query", "kept"),
    [
        ("author=5", "author=5"),
        ("passage=2&compass=n", "passage=2&compass=n"),
        ("keyword=ai&api_key=x", "keyword=ai"),
        ("X-Amz-Signature=s&X-Amz-Credential=c&design=1", "design=1"),
        ("accessToken=t&p=9", "p=9"),
        ("accesstoken=a&apitoken=b&authtoken=c&refreshtoken=d&p=1", "p=1"),
        ("clientsecret=a&secretkey=b&privatekey=c&p=1", "p=1"),
        ("sessionkey=a&SESSIONKEY=b&AccessKeyId=c&monkey=d", "monkey=d"),
        ("jsessionid=a&user_session_id=b&ASP.NET_SessionId=c&session=d&p=1", "p=1"),
    ],
)
def test_public_link_keeps_ordinary_parameters_and_drops_credentials(query: str, kept: str) -> None:
    from podcast_pipeline.cms_examples import public_cms_link

    assert public_cms_link(f"https://blog.example/ep?{query}") == f"https://blog.example/ep?{kept}"


def test_unparseable_public_link_is_dropped_not_fatal() -> None:
    (example,) = parse_cms_examples(
        [{"id": 7, "title": "Episode", "description": "<p>Notes</p>", "url": "http://ex\x00ample/"}],
        api_url="https://cms.example/api",
        limit=1,
    )
    assert example.link is None
    assert example.to_record()["link"] is None


def test_cms_html_examples_share_entity_preservation() -> None:
    literal = '<p title="&quot;">&lt;script&gt;example&lt;/script&gt; &amp;</p>'
    (example,) = parse_cms_examples(
        [{"title": "Episode", "description": literal, "shownotes": {"html": literal}}],
        api_url="https://cms.example/api",
        limit=1,
    )
    assert example.description_html == literal
    assert example.shownotes_html == literal
