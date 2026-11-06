from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from podcast_pipeline import auphonic_api
from tests.test_control_budgets import Clock, StalledStream, Stream

CREDS = auphonic_api.AuphonicCredentials("https://api.test", api_key="fictional")
OUTPUTS = [
    auphonic_api.AuphonicOutputFile(f"https://cdn.test/{name}?signature=fictional-private", name)
    for name in ["a.mp3", "b.mp3"]
]


@pytest.mark.parametrize("slow", [False, True])
def test_download_stream_budget_preserves_previous_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slow: bool,
) -> None:
    clock = Clock()
    stream = Stream(clock, slow=slow)
    monkeypatch.setattr(auphonic_api.time, "monotonic", clock.monotonic)
    monkeypatch.setenv("PODCAST_PIPELINE_DOWNLOAD_MAX_BYTES", "64" if not slow else "4096")
    monkeypatch.setenv("PODCAST_PIPELINE_DOWNLOAD_TIMEOUT_SECONDS", "2")
    dest = tmp_path / "a.mp3"
    dest.write_bytes(b"old")
    with auphonic_api.AuphonicClient(
        CREDS, transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        with pytest.raises(auphonic_api.AuphonicApiError, match="byte limit|deadline") as error:
            client.download_outputs(OUTPUTS[:1], tmp_path)
    assert "signature" not in str(error.value)
    assert stream.closed
    assert stream.count <= 3
    assert dest.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [dest]


def test_stalled_download_is_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stream = StalledStream()
    monkeypatch.setenv("PODCAST_PIPELINE_DOWNLOAD_TIMEOUT_SECONDS", "0.01")
    with auphonic_api.AuphonicClient(
        CREDS, transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        with pytest.raises(auphonic_api.AuphonicApiError, match="deadline"):
            client.download_outputs(OUTPUTS[:1], tmp_path)
    assert stream.cancelled and stream.closed
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("PODCAST_PIPELINE_DOWNLOAD_MAX_BYTES", "0"),
        ("PODCAST_PIPELINE_DOWNLOAD_MAX_BYTES", "abc"),
        ("PODCAST_PIPELINE_DOWNLOAD_TIMEOUT_SECONDS", "nan"),
        ("PODCAST_PIPELINE_DOWNLOAD_TIMEOUT_SECONDS", "inf"),
        ("PODCAST_PIPELINE_DOWNLOAD_TIMEOUT_SECONDS", "-1"),
    ],
)
def test_download_configuration_rejected_before_requests(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(auphonic_api.AuphonicApiError):
        auphonic_api.AuphonicClient(CREDS, transport=httpx.MockTransport(lambda r: pytest.fail("no requests")))


class InterruptedStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b"partial"
        raise httpx.ReadError("synthetic interruption")

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self:
            yield chunk


def test_second_download_failure_preserves_set(tmp_path: Path) -> None:
    for name in ["a.mp3", "b.mp3", "notes.txt"]:
        (tmp_path / name).write_bytes(name.encode())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/b.mp3":
            return httpx.Response(200, stream=InterruptedStream())
        return httpx.Response(200, content=b"new")

    with auphonic_api.AuphonicClient(CREDS, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(auphonic_api.AuphonicApiError):
            client.download_outputs(OUTPUTS, tmp_path)
    assert calls == ["/a.mp3", "/b.mp3"]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {
        n: n.encode() for n in ["a.mp3", "b.mp3", "notes.txt"]
    }


@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("interrupt", [False, True])
def test_publication_failure_rolls_back_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    interrupt: bool,
) -> None:
    names = ["a.mp3", "b.mp3", "notes.txt"] if existing else ["notes.txt"]
    for name in names:
        (tmp_path / name).write_bytes(name.encode())
    original = auphonic_api.os.replace
    injected = False

    def replace(source: Path, dest: Path) -> None:
        nonlocal injected
        if source.parent.name == "incoming" and source.name == "b.mp3":
            injected = True
            if interrupt:
                raise KeyboardInterrupt
            raise OSError("publication failed")
        original(source, dest)

    monkeypatch.setattr(auphonic_api.os, "replace", replace)
    with auphonic_api.AuphonicClient(
        CREDS, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"new"))
    ) as client:
        with pytest.raises(KeyboardInterrupt if interrupt else auphonic_api.AuphonicApiError):
            client.download_outputs(OUTPUTS, tmp_path)
    assert injected
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {n: n.encode() for n in names}


def test_complete_set_success_with_explicit_budgets(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_bytes(b"keep")
    with auphonic_api.AuphonicClient(
        CREDS,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=r.url.path.encode())),
        download_max_bytes=1024,
        download_timeout_seconds=10,
    ) as client:
        assert client.download_outputs(OUTPUTS, tmp_path) == (tmp_path / "a.mp3", tmp_path / "b.mp3")
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {
        "a.mp3": b"/a.mp3",
        "b.mp3": b"/b.mp3",
        "notes.txt": b"keep",
    }


def test_redirect_body_is_not_consumed(tmp_path: Path) -> None:
    class Unread(httpx.AsyncByteStream):
        closed = False
        consumed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            self.consumed = True
            yield b"redirect body must not be read"

        async def aclose(self) -> None:
            self.closed = True

    stream = Unread()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a.mp3":
            return httpx.Response(302, headers={"Location": "https://other.test/final"}, stream=stream)
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"media")

    with auphonic_api.AuphonicClient(CREDS, transport=httpx.MockTransport(handler)) as client:
        client.download_outputs(OUTPUTS[:1], tmp_path)
    assert stream.closed and not stream.consumed
    assert (tmp_path / "a.mp3").read_bytes() == b"media"


def test_rollback_failure_retains_recovery_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ["a.mp3", "b.mp3"]:
        (tmp_path / name).write_bytes(name.encode())
    original = auphonic_api.os.replace
    injected: list[str] = []

    def replace(source: Path, dest: Path) -> None:
        if source.name == "b.mp3" and source.parent.name in {"incoming", "previous"}:
            injected.append(source.parent.name)
            raise OSError("synthetic failure")
        original(source, dest)

    monkeypatch.setattr(auphonic_api.os, "replace", replace)
    with auphonic_api.AuphonicClient(
        CREDS, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"new"))
    ) as client:
        with pytest.raises(auphonic_api.AuphonicApiError, match="recovery files retained"):
            client.download_outputs(OUTPUTS, tmp_path)
    assert injected == ["incoming", "previous"]
    assert (tmp_path / "a.mp3").read_bytes() == b"a.mp3"
    stages = list(tmp_path.glob(".outputs-*"))
    assert len(stages) == 1
    assert (stages[0] / "previous" / "b.mp3").read_bytes() == b"b.mp3"


@pytest.mark.parametrize("status", [200, 500])
def test_download_errors_omit_signed_url(tmp_path: Path, status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if status == 200:
            raise httpx.ReadError(str(request.url))
        return httpx.Response(status)

    with auphonic_api.AuphonicClient(CREDS, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(auphonic_api.AuphonicApiError) as error:
            client.download_outputs(OUTPUTS[:1], tmp_path)
    assert "signature" not in str(error.value)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "headers,message",
    [
        ({"Content-Encoding": "gzip"}, "Content-Encoding"),
        ({"Content-Length": "1000000000000"}, "byte limit"),
        ({"Content-Length": "9" * 5000}, "invalid Content-Length|byte limit"),
    ],
)
def test_download_rejects_headers_before_consuming_body(tmp_path: Path, headers: dict[str, str], message: str) -> None:
    stream = Stream(Clock())
    with auphonic_api.AuphonicClient(
        CREDS, transport=httpx.MockTransport(lambda r: httpx.Response(200, headers=headers, stream=stream))
    ) as client:
        with pytest.raises(auphonic_api.AuphonicApiError, match=message) as error:
            client.download_outputs(OUTPUTS[:1], tmp_path)
    assert "a.mp3" in str(error.value)
    assert stream.count == 0 and stream.closed
    assert list(tmp_path.iterdir()) == []


def test_download_redirect_loop_is_bounded(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": str(request.url)})

    with auphonic_api.AuphonicClient(CREDS, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(auphonic_api.AuphonicApiError, match="too many redirects"):
            client.download_outputs(OUTPUTS[:1], tmp_path)
    assert calls == 21
    assert list(tmp_path.iterdir()) == []


def test_download_redirect_timeouts_shrink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(auphonic_api.time, "monotonic", clock.monotonic)
    read_limits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        read_limits.append(request.extensions["timeout"]["read"])
        clock.now += 1
        if len(read_limits) < 3:
            return httpx.Response(302, headers={"Location": "https://cdn.test/final"})
        return httpx.Response(200, content=b"audio")

    with auphonic_api.AuphonicClient(
        CREDS, transport=httpx.MockTransport(handler), download_timeout_seconds=10
    ) as client:
        client.download_outputs(OUTPUTS[:1], tmp_path)
    assert read_limits == [10, 9, 8]


@pytest.mark.parametrize("fail_download", [False, True])
def test_staging_cleanup_failure_preserves_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fail_download: bool,
) -> None:
    def cleanup(path: Path) -> None:
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(auphonic_api.shutil, "rmtree", cleanup)
    with auphonic_api.AuphonicClient(
        CREDS, transport=httpx.MockTransport(lambda r: httpx.Response(503 if fail_download else 200, content=b"new"))
    ) as client:
        if fail_download:
            with pytest.raises(auphonic_api.AuphonicApiError, match="status 503"):
                client.download_outputs(OUTPUTS[:1], tmp_path)
        else:
            assert client.download_outputs(OUTPUTS[:1], tmp_path) == (tmp_path / "a.mp3",)
            assert (tmp_path / "a.mp3").read_bytes() == b"new"
    assert "synthetic cleanup failure" in caplog.text


@pytest.mark.parametrize("publication", [False, True])
def test_filesystem_failure_retains_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication: bool,
) -> None:
    original = auphonic_api.os.replace

    def replace(source: Path, dest: Path) -> None:
        if publication == (source.parent.name == "incoming" and source.suffix != ".tmp"):
            raise OSError("synthetic disk full")
        original(source, dest)

    monkeypatch.setattr(auphonic_api.os, "replace", replace)
    with auphonic_api.AuphonicClient(
        CREDS, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"new"))
    ) as client:
        with pytest.raises(auphonic_api.AuphonicApiError, match="synthetic disk full"):
            client.download_outputs(OUTPUTS[:1], tmp_path)
    assert list(tmp_path.iterdir()) == []
