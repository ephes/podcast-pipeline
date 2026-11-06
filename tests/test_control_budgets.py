from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from podcast_pipeline import auphonic_api, cms_api


class Clock:
    now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Stream(httpx.SyncByteStream, httpx.AsyncByteStream):
    closed = False
    count = 0

    def __init__(self, clock: Clock, *, slow: bool = False) -> None:
        self.clock = clock
        self.slow = slow

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(20):
            self.count += 1
            if self.slow:
                self.clock.now += 1
            yield b" " * 32

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self:
            yield chunk

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.close()


@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize("slow", [False, True])
def test_cms_control_stream_budget(monkeypatch: pytest.MonkeyPatch, status: int, slow: bool) -> None:
    clock = Clock()
    stream = Stream(clock, slow=slow)
    requests: list[httpx.Request] = []
    monkeypatch.setattr(cms_api.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cms_api, "_MAX_CONTROL_BYTES", 64)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, stream=stream)

    with cms_api.DjangoCastClient("https://cms.test", "fictional", transport=httpx.MockTransport(handler)) as client:
        client._response_timeout = 2
        with pytest.raises(cms_api.CmsApiError, match="deadline|byte limit"):
            client.find_post(parent_id=1, slug="episode")
    assert stream.closed
    assert stream.count <= 3
    assert len(requests) == 1


def test_cms_error_details_are_bounded() -> None:
    payload = {"code": "invalid", "detail": "x" * 10000, "errors": {"title": ["y" * 10000] * 100}}
    transport = httpx.MockTransport(
        lambda r: httpx.Response(400, stream=httpx.ByteStream(json.dumps(payload).encode()))
    )
    with cms_api.DjangoCastClient("https://cms.test", "fictional", transport=transport) as client:
        with pytest.raises(cms_api.CmsApiError) as error:
            client.create_episode({})
    assert len(str(error.value)) <= 2000


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["poll_interval", "timeout_seconds"])
def test_poll_rejects_invalid_budgets(value: float, field: str) -> None:
    transport = httpx.MockTransport(lambda r: pytest.fail("no request for invalid budget"))
    with auphonic_api.AuphonicClient(
        auphonic_api.AuphonicCredentials("https://api.test", api_key="fictional"), transport=transport
    ) as client:
        kwargs = {"poll_interval": 1.0, "timeout_seconds": 10.0, field: value}
        with pytest.raises(auphonic_api.AuphonicApiError, match="finite and positive"):
            client.wait_for_production("u1", **kwargs)


@pytest.mark.parametrize("late_done", [False, True])
def test_poll_uses_total_deadline(monkeypatch: pytest.MonkeyPatch, late_done: bool) -> None:
    clock = Clock()
    monkeypatch.setattr(auphonic_api.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(auphonic_api.time, "sleep", clock.sleep)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.extensions["timeout"]["read"] <= 2
        if late_done:
            clock.now += 2
        return httpx.Response(200, json={"data": {"uuid": "u1", "status": 3 if late_done else 4}})

    with auphonic_api.AuphonicClient(
        auphonic_api.AuphonicCredentials("https://api.test", api_key="fictional"),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(auphonic_api.AuphonicApiError, match="timed out"):
            client.wait_for_production("u1", poll_interval=10, timeout_seconds=2)
    assert clock.now == 2
    assert len(requests) == 1


class StalledStream(httpx.AsyncByteStream):
    closed = False
    cancelled = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b" "
        try:
            await asyncio.sleep(60)
        finally:
            self.cancelled = True

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("cms", [False, True])
def test_stalled_io_is_cancelled(cms: bool) -> None:
    stream = StalledStream()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    if cms:
        with cms_api.DjangoCastClient(
            "https://cms.test", "fictional", response_timeout_seconds=0.01, transport=transport
        ) as client:
            with pytest.raises(cms_api.CmsApiError, match="deadline"):
                client.list_parents()
    else:
        with auphonic_api.AuphonicClient(
            auphonic_api.AuphonicCredentials("https://api.test", api_key="fictional"), transport=transport
        ) as poll_client:
            with pytest.raises(auphonic_api.AuphonicApiError, match="timed out"):
                poll_client.wait_for_production("u1", poll_interval=1, timeout_seconds=0.01)
    assert stream.cancelled
    assert stream.closed


def test_poll_retains_shorter_request_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] == 0.25
        return httpx.Response(200, json={"data": {"uuid": "u1", "status": 3}})

    with auphonic_api.AuphonicClient(
        auphonic_api.AuphonicCredentials("https://api.test", api_key="fictional"),
        timeout_seconds=0.25,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.wait_for_production("u1", poll_interval=1, timeout_seconds=60).status == 3


def test_sync_only_auphonic_transport_rejected_before_requests() -> None:
    with pytest.raises(auphonic_api.AuphonicApiError, match="both sync and async"):
        auphonic_api.AuphonicClient(
            auphonic_api.AuphonicCredentials("https://api.test", api_key="fictional"), transport=httpx.HTTPTransport()
        )


def test_cms_response_budget_does_not_limit_upload_phase() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.04)
        return httpx.Response(200, json={"id": 1})

    with cms_api.DjangoCastClient(
        "https://cms.test",
        "fictional",
        timeout_seconds=0.01,
        response_timeout_seconds=0.01,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.create_episode({}) == {"id": 1}
