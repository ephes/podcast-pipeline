from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest
import typer

from podcast_pipeline import rss_examples as rss
from podcast_pipeline.entrypoints.rss_examples import run_rss_examples

ITEM = "<item><title>One</title><description>Desc</description><guid>episode-1</guid></item>"


@pytest.mark.parametrize(
    "body",
    [
        "<x/>" * 100_001,
        "<x>" * 65 + "</x>" * 65,
        "<title>" + "x" * (4 * 1024 * 1024 + 1) + "</title>",
    ],
    ids=["elements", "depth", "text"],
)
def test_xml_work_refusal_preserves_export(body: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "examples.jsonl"
    output.write_text("old corpus")
    monkeypatch.setattr(rss, "_fetch_rss_xml", lambda **kw: "<rss><channel>" + body + ITEM + "</channel></rss>")
    with pytest.raises(typer.BadParameter, match="limit"):
        run_rss_examples(feed_url="https://feed.test/rss", output=output, limit=1, timeout_seconds=2)
    assert output.read_text() == "old corpus"


def test_dtd_is_refused() -> None:
    with pytest.raises(rss.RssExamplesError, match="DTD"):
        rss.parse_rss_examples(
            '<!DOCTYPE rss [<!ENTITY x "secret">]><rss><channel>' + ITEM.replace("Desc", "&x;") + "</channel></rss>",
            feed_url="https://feed.test/rss",
            limit=1,
        )


def test_only_requested_items_are_built(monkeypatch: pytest.MonkeyPatch) -> None:
    original = rss._parse_item
    seen = []

    def parse(item: ElementTree.Element, *, feed_url: str) -> rss.RssEpisodeExample | None:
        seen.append(item)
        return original(item, feed_url=feed_url)

    monkeypatch.setattr(rss, "_parse_item", parse)
    result = rss.parse_rss_examples(
        "<rss><channel>" + ITEM * 100 + "</channel></rss>", feed_url="https://feed.test/rss", limit=2
    )
    assert len(result) == len(seen) == 2


def test_source_is_private_in_export_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = "https://USR_SENTINEL:PWD_SENTINEL@FEED.test:443/rss?token=QUERY_SENTINEL#FRAG_SENTINEL"
    requested = []

    def fetch(**kwargs: object) -> str:
        requested.append(kwargs["feed_url"])
        return "<rss><channel>" + ITEM + "</channel></rss>"

    monkeypatch.setattr(rss, "_fetch_rss_xml", fetch)
    output = tmp_path / "examples.jsonl"
    run_rss_examples(feed_url=source, output=output, limit=1, timeout_seconds=2)
    assert requested == [source]
    rendered = output.read_text() + capsys.readouterr().out
    assert "https://feed.test/rss" in rendered
    assert "SENTINEL" not in rendered


def test_invalid_source_diagnostic_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rss, "_fetch_rss_xml", lambda **kw: "<rss><channel>" + ITEM + "</channel></rss>")
    with pytest.raises(rss.RssExamplesError) as error:
        rss.fetch_rss_examples(feed_url="https://SENTINEL@feed.test:bad/rss", limit=1)
    assert "SENTINEL" not in str(error.value)


def test_transport_error_diagnostic_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(**kwargs: object) -> str:
        raise httpx.ConnectError("https://SENTINEL@feed.test/?secret=SENTINEL")

    monkeypatch.setattr(rss, "_read_rss_xml", fail)
    with pytest.raises(rss.RssExamplesError) as error:
        rss.fetch_rss_examples(feed_url="https://SENTINEL@feed.test/rss", limit=1)
    assert "SENTINEL" not in str(error.value)
