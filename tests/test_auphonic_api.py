"""AuphonicClient start/poll/download against a mocked Auphonic JSON API.

The real Auphonic API is never called (productions cost credits): every request
goes through ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer

from podcast_pipeline import auphonic_api
from podcast_pipeline.auphonic_api import (
    AuphonicApiError,
    AuphonicClient,
    AuphonicCredentials,
    AuphonicProductionFailedError,
    _classify_status,
)
from podcast_pipeline.auphonic_payload import build_auphonic_payload
from podcast_pipeline.entrypoints import produce
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore

BASE = "https://auphonic.test/api"
CREDS = AuphonicCredentials(base_url=BASE, api_key="key")

# Codes and names from https://auphonic.com/api/info/production_status.json.
RUNNING_STATUSES = {
    0: "File Upload",
    1: "Waiting",
    4: "Audio Processing",
    5: "Audio Encoding",
    6: "Outgoing File Transfer",
    7: "Audio Mono Mixdown",
    8: "Split Audio On Chapter Marks",
    12: "Incoming File Transfer",
    13: "Stopping the Production",
    14: "Speech Recognition",
}
ERROR_STATUSES = {
    2: "Error",
    9: "Incomplete",
    11: "Production Outdated",
    98: "Empty Production",
}


def _ok(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"status_code": 200, "error_code": None, "error_message": "", "data": data})


def _production(uuid: str, status: int, status_string: str, **extra: Any) -> dict[str, Any]:
    return {"uuid": uuid, "status": status, "status_string": status_string, **extra}


class _Recorder:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self._handler(request)

    @property
    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]


def _client(recorder: _Recorder) -> AuphonicClient:
    return AuphonicClient(CREDS, transport=httpx.MockTransport(recorder))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auphonic_api.time, "sleep", lambda _seconds: None)


# --- status classification -------------------------------------------------


@pytest.mark.parametrize(("code", "name"), sorted(RUNNING_STATUSES.items()))
def test_in_progress_codes_are_running(code: int, name: str) -> None:
    assert _classify_status(code, name) == "running"


@pytest.mark.parametrize(("code", "name"), sorted(ERROR_STATUSES.items()))
def test_failure_codes_are_errors(code: int, name: str) -> None:
    assert _classify_status(code, name) == "error"


def test_done_not_started_and_changed_codes() -> None:
    assert _classify_status(3, "Done") == "done"
    assert _classify_status(10, "Production Not Started Yet") == "not_started"
    assert _classify_status(15, "Production Changed") == "changed"


def test_code_wins_over_string_and_string_is_fallback() -> None:
    assert _classify_status(2, "Something Unexpected") == "error"
    assert _classify_status(4, "Done") == "running"
    assert _classify_status("3", None) == "done"
    assert _classify_status(None, "Error") == "error"
    assert _classify_status(None, "Done") == "done"
    assert _classify_status(None, "Audio Processing") == "running"
    assert _classify_status(None, None) == "running"
    assert _classify_status(42, "Brand New State") == "running"


# --- starting productions ---------------------------------------------------


def _create_then_start(created_status: tuple[int, str] = (10, "Production Not Started Yet")) -> _Recorder:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/productions.json":
            return _ok(_production("u1", *created_status))
        if request.url.path == "/api/production/u1/upload.json":
            return _ok(_production("u1", *created_status))
        if request.url.path == "/api/production/u1/start.json":
            return _ok(_production("u1", 1, "Waiting"))
        if request.url.path == "/api/production/u1.json":
            return _ok(_production("u1", 1, "Waiting"))
        raise AssertionError(f"unexpected request {request.url}")

    return _Recorder(handler)


def test_url_input_is_created_then_started() -> None:
    recorder = _create_then_start()
    created: list[str] = []
    with _client(recorder) as client:
        production = client.start_production(
            {"preset": "p1", "title": "Ep", "input_file": "https://cdn.example/ep.mp3", "metadata": {"title": "Ep"}},
            on_created=created.append,
        )

    assert production.uuid == "u1"
    assert production.status == 1
    assert created == ["u1"]
    assert recorder.calls == [("POST", "/api/productions.json"), ("POST", "/api/production/u1/start.json")]
    body = json.loads(recorder.requests[0].content)
    assert body == {
        "preset": "p1",
        "title": "Ep",
        "input_file": "https://cdn.example/ep.mp3",
        "metadata": {"title": "Ep"},
    }


def test_api_key_is_sent_as_bearer_token() -> None:
    recorder = _create_then_start()
    with _client(recorder) as client:
        client.start_production({"preset": "p1", "input_file": "https://cdn.example/ep.mp3"})
    assert {r.headers["authorization"] for r in recorder.requests} == {"Bearer key"}


def test_username_and_password_use_basic_auth() -> None:
    recorder = _create_then_start()
    creds = AuphonicCredentials(base_url=BASE, username="user", password="secret")
    with AuphonicClient(creds, transport=httpx.MockTransport(recorder)) as client:
        client.fetch_production("u1")
    assert recorder.requests[0].headers["authorization"].startswith("Basic ")


def test_load_credentials_prefers_api_key_then_password(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AUPHONIC_API_KEY", "AUPHONIC_USER", "AUPHONIC_USERNAME", "AUPHONIC_PASSWORD", "AUPHONIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AuphonicApiError, match="Missing Auphonic credentials"):
        auphonic_api.load_auphonic_credentials()
    monkeypatch.setenv("AUPHONIC_USER", "user")
    with pytest.raises(AuphonicApiError, match="Missing Auphonic credentials"):
        auphonic_api.load_auphonic_credentials()
    monkeypatch.setenv("AUPHONIC_PASSWORD", "secret")
    assert auphonic_api.load_auphonic_credentials() == AuphonicCredentials(
        base_url="https://auphonic.com/api", username="user", password="secret"
    )
    monkeypatch.setenv("AUPHONIC_API_KEY", "k1")
    assert auphonic_api.load_auphonic_credentials() == AuphonicCredentials(
        base_url="https://auphonic.com/api", api_key="k1"
    )


def test_local_file_is_created_uploaded_then_started(tmp_path: Path) -> None:
    audio = tmp_path / "final mix.wav"
    audio.write_bytes(b"RIFFdata")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/productions.json":
            return _ok(_production("u2", 10, "Production Not Started Yet"))
        if request.url.path == "/api/production/u2/upload.json":
            return _ok(_production("u2", 10, "Production Not Started Yet"))
        if request.url.path == "/api/production/u2/start.json":
            return _ok(_production("u2", 1, "Waiting"))
        raise AssertionError(f"unexpected request {request.url}")

    recorder = _Recorder(handler)
    created: list[str] = []
    with _client(recorder) as client:
        production = client.start_production(
            {"preset": "p1", "input_file": str(audio), "chapters": [{"title": "A"}]}, on_created=created.append
        )

    assert production.uuid == "u2"
    assert production.status == 1
    assert created == ["u2"]
    assert recorder.calls == [
        ("POST", "/api/productions.json"),
        ("POST", "/api/production/u2/upload.json"),
        ("POST", "/api/production/u2/start.json"),
    ]
    create_body = json.loads(recorder.requests[0].content)
    assert create_body == {"preset": "p1", "chapters": [{"title": "A"}]}
    assert "action" not in create_body
    upload = recorder.requests[1]
    assert upload.headers["content-type"].startswith("multipart/form-data")
    assert b'name="input_file"; filename="final mix.wav"' in upload.content
    assert b"RIFFdata" in upload.content


def test_failed_upload_reports_created_uuid_and_skips_start(tmp_path: Path) -> None:
    audio = tmp_path / "mix.wav"
    audio.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/productions.json":
            return _ok(_production("u3", 10, "Production Not Started Yet"))
        return httpx.Response(
            400,
            json={"status_code": 400, "error_code": "upload", "error_message": "Unsupported file", "data": {}},
        )

    recorder = _Recorder(handler)
    with (
        _client(recorder) as client,
        pytest.raises(AuphonicApiError, match="u3 was created but starting it failed") as excinfo,
    ):
        client.start_production({"preset": "p1", "input_file": str(audio)})
    assert ("POST", "/api/production/u3/start.json") not in recorder.calls
    assert "Unsupported file" in str(excinfo.value)


def test_missing_local_file_fails_before_any_request(tmp_path: Path) -> None:
    recorder = _Recorder(lambda _r: _ok({}))
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="not found"):
        client.start_production({"preset": "p1", "input_file": str(tmp_path / "missing.wav")})
    assert recorder.calls == []


def test_multiple_input_files_are_rejected() -> None:
    recorder = _Recorder(lambda _r: _ok({}))
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="exactly one input file"):
        client.start_production(
            {"preset": "p1", "input_files": ["https://a.example/1.wav", "https://a.example/2.wav"]}
        )
    assert recorder.calls == []


def test_api_error_message_is_surfaced() -> None:
    recorder = _Recorder(
        lambda _r: httpx.Response(
            400,
            json={"status_code": 400, "error_code": "invalid", "error_message": "Preset not found", "data": {}},
        )
    )
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="Preset not found"):
        client.start_production({"preset": "nope"})


def test_transport_failure_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with _client(_Recorder(handler)) as client, pytest.raises(AuphonicApiError, match="request failed"):
        client.fetch_production("u1")


# --- polling ------------------------------------------------------------------


def _polling_handler(statuses: list[tuple[int, str]], uuid: str = "u1") -> Callable[[httpx.Request], httpx.Response]:
    remaining = iter(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/production/{uuid}.json"
        code, name = next(remaining)
        return _ok(_production(uuid, code, name))

    return handler


def test_polling_walks_through_every_in_progress_status_until_done() -> None:
    statuses = [*sorted(RUNNING_STATUSES.items()), (3, "Done")]
    recorder = _Recorder(_polling_handler(statuses))
    with _client(recorder) as client:
        production = client.wait_for_production("u1", poll_interval=0, timeout_seconds=60)
    assert production.status == 3
    assert len(recorder.requests) == len(statuses)


@pytest.mark.parametrize(("code", "name"), sorted(ERROR_STATUSES.items()))
def test_polling_raises_on_error_statuses(code: int, name: str) -> None:
    recorder = _Recorder(_polling_handler([(4, "Audio Processing"), (code, name)]))
    with _client(recorder) as client, pytest.raises(AuphonicProductionFailedError, match="--restart") as excinfo:
        client.wait_for_production("u1", poll_interval=0, timeout_seconds=60)
    assert name in str(excinfo.value)


def test_polling_raises_for_unstarted_and_changed_productions() -> None:
    with (
        _client(_Recorder(_polling_handler([(10, "Production Not Started Yet")]))) as client,
        pytest.raises(AuphonicProductionFailedError, match="never started"),
    ):
        client.wait_for_production("u1", poll_interval=0, timeout_seconds=60)
    with (
        _client(_Recorder(_polling_handler([(15, "Production Changed")]))) as client,
        pytest.raises(AuphonicProductionFailedError, match="changed after it finished"),
    ):
        client.wait_for_production("u1", poll_interval=0, timeout_seconds=60)


def test_polling_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 5.0, 11.0])
    monkeypatch.setattr(auphonic_api.time, "monotonic", lambda: next(ticks))
    recorder = _Recorder(lambda _r: _ok(_production("u1", 4, "Audio Processing")))
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="timed out"):
        client.wait_for_production("u1", poll_interval=0, timeout_seconds=10)
    assert len(recorder.requests) == 2


# --- run_produce end to end ---------------------------------------------------


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ep_001"
    workspace.mkdir()
    (workspace / "episode.yaml").write_text("episode_id: ep_001\n", encoding="utf-8")
    return workspace


class _FakeAuphonic:
    """In-memory Auphonic JSON API: each started production walks through ``script``."""

    def __init__(self, script: list[tuple[int, str]]) -> None:
        self.script = script
        self.created: list[str] = []
        self.bodies: list[dict[str, Any]] = []
        self.started: list[str] = []
        self.start_error: Callable[[httpx.Request], Exception] | None = None
        self.polls: dict[str, int] = {}
        self.recorder = _Recorder(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/productions.json":
            uuid = f"prod-{len(self.created) + 1}"
            self.created.append(uuid)
            self.bodies.append(json.loads(request.content))
            return _ok(_production(uuid, 10, "Production Not Started Yet"))
        if path.endswith("/start.json"):
            uuid = path.removeprefix("/api/production/").removesuffix("/start.json")
            self.started.append(uuid)
            if self.start_error is not None:
                raise self.start_error(request)
            return _ok(_production(uuid, 1, "Waiting"))
        if path.startswith("/api/production/") and path.endswith(".json") and path.count("/") == 3:
            uuid = path.removeprefix("/api/production/").removesuffix(".json")
            index = self.polls.get(uuid, 0)
            self.polls[uuid] = index + 1
            code, name = self.script[min(index, len(self.script) - 1)]
            outputs = [{"download_url": f"https://auphonic.test/download/{uuid}/ep.mp3", "filename": "ep.mp3"}]
            return _ok(_production(uuid, code, name, output_files=outputs if code == 3 else []))
        if path.startswith("/download/"):
            return httpx.Response(200, content=b"mp3-bytes")
        raise AssertionError(f"unexpected request {request.method} {request.url}")


@pytest.fixture()
def fake_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[list[tuple[int, str]]], _FakeAuphonic]]:
    holder: dict[str, _FakeAuphonic] = {}

    def install(script: list[tuple[int, str]]) -> _FakeAuphonic:
        fake = _FakeAuphonic(script)
        holder["fake"] = fake
        return fake

    def make_client(credentials: AuphonicCredentials) -> AuphonicClient:
        return AuphonicClient(credentials, transport=httpx.MockTransport(holder["fake"].recorder))

    monkeypatch.setattr(produce, "AuphonicClient", make_client)
    monkeypatch.setattr(produce, "load_auphonic_credentials", lambda: CREDS)
    monkeypatch.setattr(
        produce,
        "build_auphonic_payload",
        lambda **_kwargs: {"preset": "p1", "input_file": "https://cdn.example/ep.mp3"},
    )
    yield install


def test_run_produce_starts_polls_and_downloads(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([(4, "Audio Processing"), (5, "Audio Encoding"), (3, "Done")])
    workspace = _workspace(tmp_path)

    produce.run_produce(workspace=workspace, dry_run=False)

    store = EpisodeWorkspaceStore(workspace)
    assert fake.created == ["prod-1"]
    assert fake.started == ["prod-1"]
    assert fake.polls == {"prod-1": 3}
    assert (store.layout.auphonic_outputs_dir / "ep.mp3").read_bytes() == b"mp3-bytes"
    assert store.read_state().auphonic_production_uuid == "prod-1"


def test_run_produce_error_keeps_uuid_and_restart_starts_new_production(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([(4, "Audio Processing"), (2, "Error")])
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)

    with pytest.raises(typer.BadParameter, match="prod-1 failed"):
        produce.run_produce(workspace=workspace, dry_run=False)
    assert store.read_state().auphonic_production_uuid == "prod-1"

    # A plain rerun resumes the stored production and fails the same way.
    with pytest.raises(typer.BadParameter, match="--restart"):
        produce.run_produce(workspace=workspace, dry_run=False)
    assert fake.created == ["prod-1"]

    fake.script = [(4, "Audio Processing"), (3, "Done")]
    produce.run_produce(workspace=workspace, dry_run=False, restart=True)

    assert fake.created == ["prod-1", "prod-2"]
    assert fake.started == ["prod-1", "prod-2"]
    assert store.read_state().auphonic_production_uuid == "prod-2"
    assert (store.layout.auphonic_outputs_dir / "ep.mp3").exists()


def test_run_produce_keeps_uuid_when_start_outcome_is_unknown(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([(4, "Audio Processing"), (3, "Done")])
    fake.start_error = lambda request: httpx.ReadTimeout("timed out", request=request)
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)

    with pytest.raises(typer.BadParameter, match="prod-1 was created but starting it failed"):
        produce.run_produce(workspace=workspace, dry_run=False)
    assert store.read_state().auphonic_production_uuid == "prod-1"

    # Auphonic did accept the start: the rerun follows prod-1 instead of paying for another.
    fake.start_error = None
    produce.run_produce(workspace=workspace, dry_run=False)
    assert fake.created == ["prod-1"]
    assert fake.started == ["prod-1"]
    assert (store.layout.auphonic_outputs_dir / "ep.mp3").exists()


def test_run_produce_with_real_payload_builder_keeps_url_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic],
) -> None:
    monkeypatch.setattr(produce, "build_auphonic_payload", build_auphonic_payload)
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(tmp_path / "missing-config.yaml"))
    fake = fake_api([(3, "Done")])
    workspace = _workspace(tmp_path)
    (workspace / "episode.yaml").write_text(
        "episode_id: ep_001\nauphonic:\n  preset_id: p1\n  input_file: https://cdn.example/ep.mp3\n",
        encoding="utf-8",
    )

    produce.run_produce(workspace=workspace, dry_run=False)

    assert fake.bodies[0]["input_file"] == "https://cdn.example/ep.mp3"
    assert fake.bodies[0]["preset"] == "p1"
    assert fake.started == ["prod-1"]


def test_not_started_is_tolerated_during_grace_period(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 1.0, 2.0, 3.0])
    monkeypatch.setattr(auphonic_api.time, "monotonic", lambda: next(ticks))
    recorder = _Recorder(_polling_handler([(10, "Production Not Started Yet"), (1, "Waiting"), (3, "Done")]))
    with _client(recorder) as client:
        production = client.wait_for_production(
            "u1", poll_interval=0, timeout_seconds=60, not_started_grace_seconds=30
        )
    assert production.status == 3


@pytest.mark.parametrize(
    "creds",
    [
        AuphonicCredentials(base_url=BASE, api_key="key"),
        AuphonicCredentials(base_url=BASE, username="user", password="secret"),
    ],
)
def test_credentials_are_sent_only_to_the_api_origin(tmp_path: Path, creds: AuphonicCredentials) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auphonic.test" and request.url.path == "/download/redirect.mp3":
            return httpx.Response(302, headers={"Location": "https://cdn.example/final.mp3"})
        if request.url.host == "storage.example" and request.url.path == "/back.mp3":
            return httpx.Response(302, headers={"Location": "https://auphonic.test/download/back.mp3"})
        return httpx.Response(200, content=b"audio")

    recorder = _Recorder(handler)
    outputs = [
        auphonic_api.AuphonicOutputFile(url="https://auphonic.test/download/a.mp3", filename="a.mp3"),
        auphonic_api.AuphonicOutputFile(url="https://storage.example/b.mp3", filename="b.mp3"),
        auphonic_api.AuphonicOutputFile(url="http://auphonic.test/download/c.mp3", filename="c.mp3"),
        auphonic_api.AuphonicOutputFile(url="https://auphonic.test/download/redirect.mp3", filename="d.mp3"),
        auphonic_api.AuphonicOutputFile(url="https://storage.example/back.mp3", filename="e.mp3"),
    ]
    with AuphonicClient(creds, transport=httpx.MockTransport(recorder)) as client:
        client.download_outputs(outputs, tmp_path)

    sent = {str(r.url): r.headers.get("authorization") for r in recorder.requests}
    assert sent["https://auphonic.test/download/a.mp3"] is not None
    assert sent["https://auphonic.test/download/redirect.mp3"] is not None
    assert sent["https://storage.example/b.mp3"] is None
    assert sent["http://auphonic.test/download/c.mp3"] is None
    assert sent["https://cdn.example/final.mp3"] is None
    assert sent["https://storage.example/back.mp3"] is None
    assert sent["https://auphonic.test/download/back.mp3"] is not None
    assert (tmp_path / "d.mp3").read_bytes() == b"audio"


@pytest.mark.parametrize(
    "creds",
    [
        AuphonicCredentials(base_url="http://auphonic.test/api", api_key="key"),
        AuphonicCredentials(base_url="http://auphonic.test/api", username="user", password="secret"),
    ],
)
def test_same_host_scheme_change_redirect_drops_credentials(tmp_path: Path, creds: AuphonicCredentials) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http":
            return httpx.Response(301, headers={"Location": "https://auphonic.test/final.mp3"})
        return httpx.Response(200, content=b"audio")

    recorder = _Recorder(handler)
    outputs = [auphonic_api.AuphonicOutputFile(url="http://auphonic.test/download/a.mp3", filename="a.mp3")]
    with AuphonicClient(creds, transport=httpx.MockTransport(recorder)) as client:
        client.download_outputs(outputs, tmp_path)

    sent = {str(r.url): r.headers.get("authorization") for r in recorder.requests}
    assert sent["http://auphonic.test/download/a.mp3"] is not None
    assert sent["https://auphonic.test/final.mp3"] is None
