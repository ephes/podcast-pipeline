from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer

from podcast_pipeline import rss_examples as rss
from podcast_pipeline.entrypoints.rss_examples import run_rss_examples

XML = b"<rss><channel><item><title>Synthetic</title><description>Example</description></item></channel></rss>"


class FeedStream(httpx.AsyncByteStream):
    def __init__(self, *, slow: bool = False, forbidden: bool = False) -> None:
        self.slow, self.forbidden = slow, forbidden
        self.closed = False
        self.cancelled = False
        self.count = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        assert not self.forbidden, "intermediate/error bodies must not be read"
        self.count += 1
        yield b" " * 128
        try:
            if self.slow:
                await asyncio.sleep(60)
            yield XML
        finally:
            self.cancelled = self.slow

    async def aclose(self) -> None:
        self.closed = True


def mock_http(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], Any]) -> None:
    original = httpx.AsyncClient
    monkeypatch.setattr(
        rss.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    # Keep the original implementation fully synthetic when testing the red baseline.
    monkeypatch.setattr(rss.httpx, "get", lambda *args, **kwargs: httpx.Response(200, content=XML))


@pytest.mark.parametrize("slow", [False, True], ids=["oversize", "slow-drip"])
def test_rss_transfer_budget_preserves_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slow: bool,
) -> None:
    stream = FeedStream(slow=slow)
    if not slow:
        monkeypatch.setattr(rss, "MAX_RSS_BYTES", 64, raising=False)
    mock_http(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    output = tmp_path / "examples.jsonl"
    output.write_bytes(b"previous synthetic export\n")
    with pytest.raises(typer.BadParameter, match="byte limit|deadline"):
        run_rss_examples(feed_url="https://feed.test/rss", output=output, limit=2, timeout_seconds=0.02 if slow else 2)
    assert stream.closed
    assert stream.count == 1
    assert stream.cancelled == slow
    assert output.read_bytes() == b"previous synthetic export\n"


def test_rss_redirects_stream_and_close_without_reading_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    redirected = FeedStream(forbidden=True)
    final = FeedStream()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["accept-encoding"] == "identity"
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": "/next"}, stream=redirected)
        return httpx.Response(200, stream=final)

    mock_http(monkeypatch, handler)
    examples = rss.fetch_rss_examples(feed_url="https://feed.test/rss", limit=1)
    assert examples[0].title == "Synthetic"
    assert len(requests) == 2
    assert redirected.closed and redirected.count == 0
    assert final.closed


def test_rss_redirect_headers_share_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)
        return httpx.Response(302, headers={"Location": "/next"})

    mock_http(monkeypatch, handler)
    with pytest.raises(rss.RssExamplesError, match="deadline"):
        rss.fetch_rss_examples(feed_url="https://feed.test/rss", limit=1, timeout_seconds=0.02)
    assert cancelled == [True]


@pytest.mark.parametrize("kind", ["http-error", "compression", "redirect-loop"])
def test_rss_refusals_close_without_body_reads(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    streams: list[FeedStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stream = FeedStream(forbidden=True)
        streams.append(stream)
        if kind == "http-error":
            return httpx.Response(500, stream=stream)
        if kind == "compression":
            return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)
        return httpx.Response(302, headers={"Location": "/loop"}, stream=stream)

    mock_http(monkeypatch, handler)
    with pytest.raises(rss.RssExamplesError, match="HTTP 500|Content-Encoding|redirect limit"):
        rss.fetch_rss_examples(feed_url="https://feed.test/rss", limit=1)
    assert streams
    assert all(stream.closed and stream.count == 0 for stream in streams)
    assert len(streams) <= 6


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_rss_invalid_deadline_before_request(monkeypatch: pytest.MonkeyPatch, timeout: float) -> None:
    mock_http(monkeypatch, lambda request: pytest.fail("invalid deadline must not request"))
    with pytest.raises(rss.RssExamplesError, match="finite and positive"):
        rss.fetch_rss_examples(feed_url="https://feed.test/rss", limit=1, timeout_seconds=timeout)


def test_rss_normal_export_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_http(monkeypatch, lambda request: httpx.Response(200, stream=FeedStream()))
    output = tmp_path / "examples.jsonl"

    def export() -> bytes:
        run_rss_examples(feed_url="https://feed.test/rss", output=output, limit=2, timeout_seconds=2)
        return output.read_bytes()

    assert export() == export()


def test_rss_late_headers_rejected_even_without_yield(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(rss, "monotonic", lambda: now[0], raising=False)
    stream = FeedStream()

    def handler(request: httpx.Request) -> httpx.Response:
        now[0] = 3
        return httpx.Response(200, stream=stream)

    mock_http(monkeypatch, handler)
    with pytest.raises(rss.RssExamplesError, match="deadline"):
        rss.fetch_rss_examples(feed_url="https://feed.test/rss", limit=1, timeout_seconds=2)
    assert stream.closed and stream.count == 0


def test_rss_connection_error_names_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic private detail", request=request)

    mock_http(monkeypatch, handler)
    with pytest.raises(rss.RssExamplesError, match="ConnectError") as error:
        rss.fetch_rss_examples(feed_url="https://feed.test/rss", limit=1)
    assert "private detail" not in str(error.value)
