"""Admission of Auphonic control responses and of preset data before paid or mutating requests.

Control responses (presets, create/upload/start/update/delete answers, status
polls) are streamed under a decoded byte ceiling; malformed preset data stops
a run before anything is created or changed. Every request goes through
``httpx.MockTransport``.
"""

from __future__ import annotations

import gzip
import json
import zlib
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer

from podcast_pipeline import auphonic_api
from podcast_pipeline.auphonic_api import (
    CONTROL_MAX_BYTES,
    AuphonicApiError,
    AuphonicClient,
    AuphonicCredentials,
    AuphonicNotFoundError,
)
from podcast_pipeline.entrypoints import produce
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore
from tests.test_auphonic_api import (
    BASE,
    CREDS,
    _FakeAuphonic,
    _ok,
    _production,
    _Recorder,
    _rerun_payload,
    _stored,
    _workspace,
)

LIMIT = 4096


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auphonic_api.time, "sleep", lambda _seconds: None)


class _Stream(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A streamed (not pre-read) body that records how much was pulled and whether it was closed."""

    def __init__(self, chunks: Iterator[bytes] | list[bytes]) -> None:
        self._chunks = iter(chunks)
        self.pulled = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self.pulled += len(chunk)
            yield chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self:
            yield chunk

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True


def _endless(chunk: bytes) -> Iterator[bytes]:
    while True:
        yield chunk


def _streamed(status: int, chunks: Iterator[bytes] | list[bytes], **headers: str) -> tuple[httpx.Response, _Stream]:
    stream = _Stream(chunks)
    return httpx.Response(status, headers=headers, stream=stream), stream


def _ok_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps({"status_code": 200, "error_message": "", "data": data}).encode()


def _client(handler: Callable[[httpx.Request], httpx.Response], *, limit: int = LIMIT) -> AuphonicClient:
    return AuphonicClient(CREDS, transport=httpx.MockTransport(handler), control_max_bytes=limit)


def _payload() -> dict[str, Any]:
    return {
        "preset": "p1",
        "is_multitrack": True,
        "multi_input_files": [{"type": "multitrack", "id": "jochen", "input_file": "https://cdn.example/j.flac"}],
    }


def _preset_ok() -> httpx.Response:
    return _ok({"uuid": "p1", "is_multitrack": True})


# --- control response admission ---------------------------------------------------


def test_default_ceiling_is_documented_and_validated() -> None:
    assert CONTROL_MAX_BYTES == 8 * 1024 * 1024
    for bad in (0, -1, True, 1.5):
        with pytest.raises(AuphonicApiError, match="control_max_bytes"):
            AuphonicClient(CREDS, control_max_bytes=bad)  # type: ignore[arg-type]


def test_oversized_preset_stream_fails_closes_and_creates_nothing() -> None:
    streams: list[_Stream] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        response, stream = _streamed(200, _endless(b" " * 1024))
        streams.append(stream)
        return response

    recorder = _Recorder(handler)
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="exceeds the 4096-byte limit"):
        client.start_production(_payload())
    assert recorder.calls == [("GET", "/api/preset/p1.json")]
    assert streams[0].closed
    assert streams[0].pulled <= LIMIT + 1024


def test_declared_oversized_length_fails_before_reading() -> None:
    response, stream = _streamed(200, [b"x" * 10], **{"Content-Length": str(LIMIT + 1)})
    with _client(lambda _r: response) as client, pytest.raises(AuphonicApiError, match="byte limit|byte-limit"):
        client.fetch_production("u1")
    assert stream.pulled == 0
    assert stream.closed


def test_gzip_bomb_is_bounded_by_the_decoded_ceiling() -> None:
    bomb = gzip.compress(b" " * (64 * 1024 * 1024))
    assert len(bomb) < 128 * 1024
    response, stream = _streamed(200, [bomb], **{"Content-Encoding": "gzip"})
    with _client(lambda _r: response) as client, pytest.raises(AuphonicApiError, match="exceeds the 4096-byte limit"):
        client.fetch_production("u1")
    assert stream.closed


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_compressed_control_responses_within_the_ceiling_are_accepted(encoding: str) -> None:
    raw = _ok_bytes(_production("u1", 3, "Done"))
    body = gzip.compress(raw) if encoding == "gzip" else zlib.compress(raw)
    half = len(body) // 2
    response, _stream = _streamed(200, [body[:half], body[half:]], **{"Content-Encoding": encoding})
    with _client(lambda _r: response) as client:
        assert client.fetch_production("u1").uuid == "u1"


def test_control_requests_only_advertise_bounded_encodings() -> None:
    recorder = _Recorder(lambda _r: _ok(_production("u1", 3, "Done")))
    with _client(recorder) as client:
        client.fetch_production("u1")
        client.fetch_production("u1", timeout_seconds=5)
    assert [r.headers["Accept-Encoding"] for r in recorder.requests] == ["gzip, deflate", "gzip, deflate"]


@pytest.mark.parametrize(
    ("headers", "chunks", "message"),
    [
        ({"Content-Encoding": "br"}, [b"\x00"], "unsupported Content-Encoding"),
        ({"Content-Encoding": "gzip, gzip"}, [b"\x00"], "unsupported Content-Encoding"),
        ({"Content-Encoding": "gzip"}, [gzip.compress(b'{"data": {}}')[:-6]], "truncated compressed body"),
        ({"Content-Encoding": "gzip"}, [b"not gzip at all"], "invalid compressed body"),
        ({"Content-Encoding": "gzip"}, [gzip.compress(b'{"data": {}}') + b"junk"], "invalid compressed body"),
        (
            {"Content-Encoding": "gzip"},
            [gzip.compress(b'{"data": {}}'), gzip.compress(b"{}")],
            "invalid compressed body",
        ),
        ({"Content-Encoding": "deflate"}, [zlib.compress(b'{"data": {}}') + b"x"], "invalid compressed body"),
    ],
)
def test_bad_encodings_are_api_errors(headers: dict[str, str], chunks: list[bytes], message: str) -> None:
    response, stream = _streamed(200, chunks, **headers)
    with _client(lambda _r: response) as client, pytest.raises(AuphonicApiError, match=message):
        client.fetch_production("u1")
    assert stream.closed


@pytest.mark.parametrize(
    "body",
    [
        b"[" * 100_000 + b"]" * 100_000,
        b'{"data": {"uuid": "u1", "status": ' + b"9" * 5000 + b"}}",
        b'{"data": {"uuid": "u1", "status": NaN}}',
        b'{"data": {"uuid": "u1", "status": Infinity}}',
        b'{"data": {"uuid": "u1", "status": 1e999}}',
        b'{"data": {"uuid": "\xff\xfe"}}',
        '{"data": {"uuid": "u1", "status": 3}}'.encode("utf-16"),
        '{"data": {"uuid": "u1", "status": 3}}'.encode("utf-32"),
        b'\xef\xbb\xbf{"data": {"uuid": "u1", "status": 3}}',
        b"",
    ],
    ids=["deep", "huge-int", "nan", "infinity", "overflow", "bad-utf8", "utf16", "utf32", "bom", "empty"],
)
@pytest.mark.parametrize("polling", [False, True])
def test_malformed_control_json_is_an_api_error(body: bytes, polling: bool) -> None:
    with _client(lambda _r: httpx.Response(200, stream=_Stream([body])), limit=CONTROL_MAX_BYTES) as client:
        with pytest.raises(AuphonicApiError, match="invalid JSON"):
            client.fetch_production("u1", timeout_seconds=5 if polling else None)


def test_oversized_polling_response_fails_and_closes() -> None:
    response, stream = _streamed(200, _endless(b" " * 1024))
    with _client(lambda _r: response) as client, pytest.raises(AuphonicApiError, match="exceeds the 4096-byte limit"):
        client.wait_for_production("u1", poll_interval=1, timeout_seconds=30)
    assert stream.closed


def test_oversized_start_response_keeps_created_uuid_and_never_starts_twice() -> None:
    created: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/preset/p1.json":
            return _preset_ok()
        if request.url.path == "/api/productions.json":
            return _ok(_production("u1", 10, "Production Not Started Yet"))
        if request.url.path == "/api/production/u1/start.json":
            return _streamed(200, _endless(b" " * 1024))[0]
        raise AssertionError(f"unexpected request {request.url}")

    recorder = _Recorder(handler)
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="u1 was created but starting it failed"):
        client.start_production(_payload(), on_created=created.append)
    assert created == ["u1"]
    assert recorder.calls == [
        ("GET", "/api/preset/p1.json"),
        ("POST", "/api/productions.json"),
        ("POST", "/api/production/u1/start.json"),
    ]


def test_oversized_create_response_reports_no_uuid_and_does_not_start() -> None:
    created: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/preset/p1.json":
            return _preset_ok()
        return _streamed(200, _endless(b" " * 1024))[0]

    recorder = _Recorder(handler)
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="exceeds"):
        client.start_production(_payload(), on_created=created.append)
    assert created == []
    assert recorder.calls == [("GET", "/api/preset/p1.json"), ("POST", "/api/productions.json")]


@pytest.mark.parametrize("status", [200, 204])
def test_empty_delete_success_is_accepted_when_streamed(status: int) -> None:
    with _client(lambda _r: httpx.Response(status, stream=_Stream([b"", b"  \n"]))) as client:
        client.delete_production("u1")


def test_oversized_delete_response_is_an_error() -> None:
    with _client(lambda _r: _streamed(200, _endless(b" " * 1024))[0]) as client:
        with pytest.raises(AuphonicApiError, match="exceeds"):
            client.delete_production("u1")


def test_oversized_404_body_is_bounded() -> None:
    with _client(lambda _r: _streamed(404, _endless(b" " * 1024))[0]) as client:
        with pytest.raises(AuphonicApiError, match="exceeds") as excinfo:
            client.fetch_production("u1")
    assert not isinstance(excinfo.value, AuphonicNotFoundError)


def test_small_404_is_still_not_found() -> None:
    body = json.dumps({"status_code": 404, "error_message": "Production not found"}).encode()
    with _client(lambda _r: httpx.Response(404, stream=_Stream([body]))) as client:
        with pytest.raises(AuphonicNotFoundError, match="Production not found"):
            client.fetch_production("u1")


@pytest.mark.parametrize("polling", [False, True])
def test_control_redirects_skip_their_bodies_and_keep_the_origin_policy(polling: bool) -> None:
    redirect_streams: list[_Stream] = []
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("Authorization")))
        if request.url.host == "auphonic.test":
            stream = _Stream(_endless(b"x" * 1024))
            redirect_streams.append(stream)
            return httpx.Response(302, headers={"Location": "https://mirror.example/u1.json"}, stream=stream)
        return _ok(_production("u1", 3, "Done"))

    with _client(handler) as client:
        assert client.fetch_production("u1", timeout_seconds=5 if polling else None).uuid == "u1"
    assert seen == [(f"{BASE}/production/u1.json", "Bearer key"), ("https://mirror.example/u1.json", None)]
    assert redirect_streams[0].pulled == 0
    assert redirect_streams[0].closed


def test_redirect_loops_are_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": str(request.url)})

    with _client(handler) as client, pytest.raises(AuphonicApiError, match="too many redirects"):
        client.fetch_production("u1")


# --- preset proof before mutation --------------------------------------------------


def _preset_handler(preset: object) -> _Recorder:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/preset/p1.json":
            return httpx.Response(200, json={"status_code": 200, "data": preset})
        if request.url.path == "/api/preset/legacy.json":
            return httpx.Response(200, json={"status_code": 200, "data": preset})
        if request.url.path == "/api/productions.json":
            return _ok(_production("u1", 10, "Production Not Started Yet"))
        if request.url.path == "/api/production/u1/start.json":
            return _ok(_production("u1", 1, "Waiting"))
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    return _Recorder(handler)


MALFORMED_PRESETS: list[tuple[object, str]] = [
    ([], "no readable data"),
    ("p1", "no readable data"),
    (None, "no readable data"),
    ({"uuid": "p1", "is_multitrack": None}, "invalid is_multitrack"),
    ({"uuid": "p1", "is_multitrack": "true"}, "invalid is_multitrack"),
    ({"uuid": "p1", "is_multitrack": 1}, "invalid is_multitrack"),
    ({"uuid": "p1", "is_multitrack": True, "multi_input_files": "jochen"}, "unreadable multi_input_files"),
    ({"uuid": "p1", "is_multitrack": True, "multi_input_files": {"id": "jochen"}}, "unreadable multi_input_files"),
    ({"uuid": "p1", "is_multitrack": True, "multi_input_files": ["jochen"]}, "unreadable multi_input_files"),
    ({"uuid": "p1", "is_multitrack": True, "multi_input_files": [{"type": 5}]}, "unreadable multi_input_files"),
]


@pytest.mark.parametrize(("preset", "message"), MALFORMED_PRESETS)
def test_malformed_preset_stops_creation_before_any_mutation(preset: object, message: str) -> None:
    created: list[str] = []
    recorder = _preset_handler(preset)
    with (
        AuphonicClient(CREDS, transport=httpx.MockTransport(recorder)) as client,
        pytest.raises(AuphonicApiError, match=message),
    ):
        client.start_production(_payload(), on_created=created.append)
    assert recorder.calls == [("GET", "/api/preset/p1.json")]
    assert created == []


@pytest.mark.parametrize(("preset", "message"), MALFORMED_PRESETS)
def test_malformed_preset_stops_a_rerun_before_any_mutation(tmp_path: Path, preset: object, message: str) -> None:
    updates: list[str] = []
    recorder = _preset_handler(preset)
    with (
        AuphonicClient(CREDS, transport=httpx.MockTransport(recorder)) as client,
        pytest.raises(AuphonicApiError, match=message),
    ):
        client.rerun_production(_stored(), _rerun_payload(tmp_path), before_update=lambda: updates.append("x"))
    assert recorder.calls == [("GET", "/api/preset/p1.json")]
    assert updates == []


def test_legacy_preset_without_is_multitrack_is_single_track() -> None:
    preset = {"uuid": "legacy", "preset_name": "Old mono preset"}
    single = _preset_handler(preset)
    with AuphonicClient(CREDS, transport=httpx.MockTransport(single)) as client:
        client.start_production({"preset": "legacy", "input_file": "https://cdn.example/final_mix.mp3"})
    assert ("POST", "/api/production/u1/start.json") in single.calls

    multi = _preset_handler(preset)
    with (
        AuphonicClient(CREDS, transport=httpx.MockTransport(multi)) as client,
        pytest.raises(AuphonicApiError, match="single-track preset"),
    ):
        client.start_production({**_payload(), "preset": "legacy"})
    assert multi.calls == [("GET", "/api/preset/legacy.json")]


@pytest.mark.parametrize(
    "preset",
    [
        {"uuid": "p1", "is_multitrack": True},
        {"uuid": "p1", "is_multitrack": True, "multi_input_files": None},
        {"uuid": "p1", "is_multitrack": True, "multi_input_files": []},
        {"uuid": "p1", "is_multitrack": True, "multi_input_files": [{"type": "intro", "id": "intro"}]},
        {"uuid": "p1", "is_multitrack": True, "multi_input_files": [{"type": "Outro"}]},
    ],
)
def test_valid_multitrack_presets_pass(preset: dict[str, Any]) -> None:
    recorder = _preset_handler(preset)
    with AuphonicClient(CREDS, transport=httpx.MockTransport(recorder)) as client:
        client.start_production(_payload())
    assert ("POST", "/api/production/u1/start.json") in recorder.calls


def test_preset_track_without_type_still_counts_as_speaker_track() -> None:
    recorder = _preset_handler({"uuid": "p1", "is_multitrack": True, "multi_input_files": [{"type": None}]})
    with (
        AuphonicClient(CREDS, transport=httpx.MockTransport(recorder)) as client,
        pytest.raises(AuphonicApiError, match=r"contains multitrack tracks #1"),
    ):
        client.start_production(_payload())
    assert recorder.calls == [("GET", "/api/preset/p1.json")]


def test_produce_update_with_malformed_preset_keeps_downloaded_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeAuphonic([(3, "Done")])

    def make_client(credentials: AuphonicCredentials) -> AuphonicClient:
        return AuphonicClient(credentials, transport=httpx.MockTransport(fake.recorder))

    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(tmp_path / "missing-config.yaml"))
    monkeypatch.setattr(produce, "AuphonicClient", make_client)
    monkeypatch.setattr(produce, "load_auphonic_credentials", lambda: CREDS)
    monkeypatch.setattr(produce, "build_auphonic_payload", lambda **_kwargs: _payload())
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    produce.run_produce(workspace=workspace, dry_run=False)
    before = store.read_state()
    assert before.auphonic_productions[-1].outputs_downloaded_at is not None

    dispatch = fake.recorder._handler

    def malformed_preset(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/preset/"):
            return httpx.Response(200, json={"status_code": 200, "data": {"uuid": "p1", "is_multitrack": "yes"}})
        return dispatch(request)

    monkeypatch.setattr(fake.recorder, "_handler", malformed_preset)
    with pytest.raises(typer.BadParameter, match="invalid is_multitrack"):
        produce.run_produce(workspace=workspace, dry_run=False, update=True)

    assert fake.updates == []
    assert fake.track_updates == []
    assert fake.started == ["prod-1"]
    assert store.read_state().auphonic_productions == before.auphonic_productions
