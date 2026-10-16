"""AuphonicClient start/poll/download against a mocked Auphonic JSON API.

The real Auphonic API is never called (productions cost credits): every request
goes through ``httpx.MockTransport``.
"""

from __future__ import annotations

import itertools
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
    AuphonicProduction,
    AuphonicProductionFailedError,
    AuphonicTrack,
    _classify_status,
    update_requests,
)
from podcast_pipeline.auphonic_payload import build_auphonic_payload
from podcast_pipeline.domain.models import EpisodeWorkspace
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


def _preset(
    uuid: str = "p1", *, multitrack: bool = False, tracks: list[dict[str, Any]] | None = None
) -> httpx.Response:
    data: dict[str, Any] = {"uuid": uuid, "preset_name": "Podcast", "is_multitrack": multitrack}
    if tracks is not None:
        data["multi_input_files"] = tracks
    return _ok(data)


def _create_then_start(created_status: tuple[int, str] = (10, "Production Not Started Yet")) -> _Recorder:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/preset/p1.json":
            return _preset()
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
    assert recorder.calls == [
        ("GET", "/api/preset/p1.json"),
        ("POST", "/api/productions.json"),
        ("POST", "/api/production/u1/start.json"),
    ]
    body = json.loads(recorder.requests[1].content)
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
        if request.url.path == "/api/preset/p1.json":
            return _preset()
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
        ("GET", "/api/preset/p1.json"),
        ("POST", "/api/productions.json"),
        ("POST", "/api/production/u2/upload.json"),
        ("POST", "/api/production/u2/start.json"),
    ]
    create_body = json.loads(recorder.requests[1].content)
    assert create_body == {"preset": "p1", "chapters": [{"title": "A"}]}
    assert "action" not in create_body
    upload = recorder.requests[2]
    assert upload.headers["content-type"].startswith("multipart/form-data")
    assert b'name="input_file"; filename="final mix.wav"' in upload.content
    assert b"RIFFdata" in upload.content


def test_failed_upload_reports_created_uuid_and_skips_start(tmp_path: Path) -> None:
    audio = tmp_path / "mix.wav"
    audio.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/preset/p1.json":
            return _preset()
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


def _multitrack_handler(
    *, preset_multitrack: bool = True, preset_tracks: list[dict[str, Any]] | None = None
) -> _Recorder:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/preset/mt.json":
            return _preset("mt", multitrack=preset_multitrack, tracks=preset_tracks)
        if request.url.path == "/api/productions.json":
            return _ok(_production("m1", 10, "Production Not Started Yet", is_multitrack=True))
        if request.url.path == "/api/production/m1/upload.json":
            return _ok(_production("m1", 10, "Production Not Started Yet", is_multitrack=True))
        if request.url.path == "/api/production/m1/start.json":
            return _ok(_production("m1", 1, "Waiting", is_multitrack=True))
        raise AssertionError(f"unexpected request {request.url}")

    return _Recorder(handler)


def _multitrack_payload(*stems: dict[str, Any]) -> dict[str, Any]:
    return {
        "preset": "mt",
        "title": "Ep",
        "is_multitrack": True,
        "multi_input_files": list(stems),
        "metadata": {"title": "Ep"},
    }


def test_multitrack_stems_are_uploaded_as_one_form_field_per_track(tmp_path: Path) -> None:
    jochen = tmp_path / "01-jochen.flac"
    jochen.write_bytes(b"fLaC-jochen")
    dominik = tmp_path / "02-dominik.flac"
    dominik.write_bytes(b"fLaC-dominik")
    recorder = _multitrack_handler()
    created: list[str] = []

    with _client(recorder) as client:
        production = client.start_production(
            _multitrack_payload(
                {"type": "multitrack", "id": "jochen", "input_file": str(jochen), "algorithms": {"denoise": True}},
                {"type": "multitrack", "id": "dominik", "input_file": str(dominik), "offset": 1.5},
                {"type": "multitrack", "id": "johannes", "input_file": "https://cdn.example/johannes.flac"},
            ),
            on_created=created.append,
        )

    assert production.uuid == "m1"
    assert production.is_multitrack is True
    assert created == ["m1"]
    assert recorder.calls == [
        ("GET", "/api/preset/mt.json"),
        ("POST", "/api/productions.json"),
        ("POST", "/api/production/m1/upload.json"),
        ("POST", "/api/production/m1/start.json"),
    ]
    create_body = json.loads(recorder.requests[1].content)
    assert create_body == {
        "preset": "mt",
        "title": "Ep",
        "is_multitrack": True,
        "multi_input_files": [
            {"type": "multitrack", "id": "jochen", "algorithms": {"denoise": True}},
            {"type": "multitrack", "id": "dominik", "offset": 1.5},
            {"type": "multitrack", "id": "johannes", "input_file": "https://cdn.example/johannes.flac"},
        ],
        "metadata": {"title": "Ep"},
    }
    upload = recorder.requests[2]
    assert upload.headers["content-type"].startswith("multipart/form-data")
    assert upload.content.count(b"Content-Disposition: form-data;") == 2
    assert b'name="jochen"; filename="01-jochen.flac"' in upload.content
    assert b'name="dominik"; filename="02-dominik.flac"' in upload.content
    assert b"fLaC-jochen" in upload.content
    assert b"fLaC-dominik" in upload.content
    assert b"johannes" not in upload.content


def test_multitrack_url_stems_skip_the_upload() -> None:
    recorder = _multitrack_handler()
    with _client(recorder) as client:
        client.start_production(
            _multitrack_payload({"type": "multitrack", "id": "jochen", "input_file": "https://cdn.example/j.flac"})
        )
    assert ("POST", "/api/production/m1/upload.json") not in recorder.calls
    assert ("POST", "/api/production/m1/start.json") in recorder.calls


def test_missing_stem_fails_before_any_request(tmp_path: Path) -> None:
    recorder = _multitrack_handler()
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="not found"):
        client.start_production(
            _multitrack_payload({"type": "multitrack", "id": "jochen", "input_file": str(tmp_path / "missing.flac")})
        )
    assert recorder.calls == []


def test_multitrack_payload_rejects_single_input_file() -> None:
    recorder = _multitrack_handler()
    payload = _multitrack_payload({"type": "multitrack", "id": "jochen", "input_file": "https://cdn.example/j.flac"})
    payload["input_file"] = "https://cdn.example/final_mix.mp3"
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="multi_input_files only"):
        client.start_production(payload)
    assert recorder.calls == []


def test_single_track_preset_is_rejected_for_multitrack_before_create() -> None:
    recorder = _multitrack_handler(preset_multitrack=False)
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="single-track preset"):
        client.start_production(
            _multitrack_payload({"type": "multitrack", "id": "jochen", "input_file": "https://cdn.example/j.flac"})
        )
    assert recorder.calls == [("GET", "/api/preset/mt.json")]


@pytest.mark.parametrize(
    ("preset_tracks", "message"),
    [
        (
            [{"type": "multitrack", "id": "jochen"}, {"type": "multitrack", "id": "dominik"}],
            r"contains multitrack tracks jochen, dominik \(colliding with auphonic.tracks: jochen\)",
        ),
        ([{"type": "multitrack", "id": "guest"}], r"contains multitrack tracks guest\. Auphonic adds"),
        ([{"id": "jochen"}], "colliding with auphonic.tracks: jochen"),
    ],
)
def test_preset_with_speaker_tracks_is_rejected_before_create(
    preset_tracks: list[dict[str, Any]], message: str
) -> None:
    recorder = _multitrack_handler(preset_tracks=preset_tracks)
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match=message) as excinfo:
        client.start_production(
            _multitrack_payload({"type": "multitrack", "id": "jochen", "input_file": "https://cdn.example/j.flac"})
        )
    assert "auphonic.tracks[].algorithms" in str(excinfo.value)
    assert recorder.calls == [("GET", "/api/preset/mt.json")]


def test_preset_intro_and_outro_tracks_are_allowed() -> None:
    recorder = _multitrack_handler(
        preset_tracks=[
            {"type": "intro", "id": "intro", "input_file": "https://cdn.example/intro.flac"},
            {"type": "outro", "id": "outro", "input_file": "https://cdn.example/outro.flac"},
        ]
    )
    with _client(recorder) as client:
        client.start_production(
            _multitrack_payload({"type": "multitrack", "id": "jochen", "input_file": "https://cdn.example/j.flac"})
        )
    assert ("POST", "/api/production/m1/start.json") in recorder.calls


def test_multitrack_preset_is_rejected_for_single_track_before_create() -> None:
    recorder = _multitrack_handler(preset_multitrack=True)
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="multitrack preset"):
        client.start_production({"preset": "mt", "input_file": "https://cdn.example/final_mix.mp3"})
    assert recorder.calls == [("GET", "/api/preset/mt.json")]


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


# --- re-running an existing production ------------------------------------------


def _stored(
    status: tuple[int, str] = (3, "Done"),
    *,
    tracks: tuple[tuple[str, str | None], ...] = (("jochen", "jochen.flac"), ("dominik", "dominik.flac")),
    **extra: Any,
) -> AuphonicProduction:
    extra.setdefault("change_allowed", True)
    return AuphonicProduction(
        uuid="u1",
        status=status[0],
        status_string=status[1],
        output_files=(),
        is_multitrack=True,
        tracks=tuple(AuphonicTrack(id=track_id, type="multitrack", input_file=name) for track_id, name in tracks),
        **extra,
    )


def _rerun_payload(tmp_path: Path) -> dict[str, Any]:
    return {
        "preset": "p1",
        "title": "Episode 1",
        "is_multitrack": True,
        "multi_input_files": [
            {"type": "multitrack", "id": "jochen", "input_file": str(tmp_path / "jochen.flac"), "offset": 1.5},
            {
                "type": "multitrack",
                "id": "dominik",
                "input_file": str(tmp_path / "dominik.flac"),
                "algorithms": {"denoise": True},
            },
        ],
        "output_basename": "ep_001",
        "metadata": {"title": "Episode 1", "year": 2026},
        "chapters": [{"start": "00:00:00.000", "title": "Intro"}, {"start": "00:01:00.000", "title": "Main"}],
    }


def _rerun_handler(
    *, chapters_after_update: list[dict[str, Any]] | None = None, start_status: int = 1
) -> Callable[[httpx.Request], httpx.Response]:
    state: dict[str, Any] = {"chapters": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/preset/p1.json":
            return _preset(multitrack=True)
        if path == "/api/production/u1.json" and request.method == "POST":
            body = json.loads(request.content)
            state["chapters"] = chapters_after_update if chapters_after_update is not None else body["chapters"]
        if path == "/api/production/u1/start.json":
            return _ok(_production("u1", start_status, "Waiting"))
        if path.startswith("/api/production/u1"):
            return _ok(
                _production(
                    "u1",
                    15,
                    "Production Changed",
                    chapters=state["chapters"],
                    output_files=[{"format": "mp3", "download_url": None}],
                )
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    return handler


def test_update_requests_drop_inputs_and_keep_per_track_settings(tmp_path: Path) -> None:
    body, tracks = update_requests(_rerun_payload(tmp_path))

    assert body == {
        "preset": "p1",
        "title": "Episode 1",
        "output_basename": "ep_001",
        "metadata": {"title": "Episode 1", "year": 2026},
        "chapters": [{"start": "00:00:00.000", "title": "Intro"}, {"start": "00:01:00.000", "title": "Main"}],
    }
    assert tracks == [
        {"type": "multitrack", "id": "jochen", "offset": 1.5},
        {"type": "multitrack", "id": "dominik", "offset": 0, "algorithms": {"denoise": True}},
    ]
    single_body, single_tracks = update_requests({"preset": "p1", "input_file": "https://cdn.example/ep.mp3"})
    assert single_body == {"preset": "p1"}
    assert single_tracks == []


def test_rerun_updates_tracks_and_starts_without_uploading(tmp_path: Path) -> None:
    recorder = _Recorder(_rerun_handler())
    with _client(recorder) as client:
        started = client.rerun_production(_stored(), _rerun_payload(tmp_path))

    assert started.status == 1
    assert recorder.calls == [
        ("GET", "/api/preset/p1.json"),
        ("POST", "/api/production/u1.json"),
        ("POST", "/api/production/u1/multi_input_files/jochen.json"),
        ("POST", "/api/production/u1/multi_input_files/dominik.json"),
        ("GET", "/api/production/u1.json"),
        ("POST", "/api/production/u1/start.json"),
    ]
    update_body = json.loads(recorder.requests[1].content)
    assert "multi_input_files" not in update_body
    assert "is_multitrack" not in update_body
    assert update_body["preset"] == "p1"
    assert json.loads(recorder.requests[3].content) == {
        "type": "multitrack",
        "id": "dominik",
        "offset": 0,
        "algorithms": {"denoise": True},
    }


@pytest.mark.parametrize(
    ("stored", "match"),
    [
        (_stored(tracks=(("jochen", "jochen.flac"),)), "has the tracks jochen, but episode.yaml configures"),
        (
            _stored(tracks=(("jochen", "jochen-old.flac"), ("dominik", "dominik.flac"))),
            "Track jochen .* was produced from jochen-old.flac",
        ),
        (_stored(tracks=(("jochen", None), ("dominik", "dominik.flac"))), "Track jochen .* has no input file"),
        (_stored((4, "Audio Processing")), "still running"),
        (_stored(change_allowed=False), "does not allow changes"),
    ],
)
def test_rerun_refuses_before_any_request(tmp_path: Path, stored: AuphonicProduction, match: str) -> None:
    recorder = _Recorder(_rerun_handler())
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match=match):
        client.rerun_production(stored, _rerun_payload(tmp_path))
    assert recorder.calls == []


def test_rerun_refuses_a_single_track_production_for_a_multitrack_payload(tmp_path: Path) -> None:
    stored = AuphonicProduction(uuid="u1", status=3, status_string="Done", output_files=(), is_multitrack=False)
    recorder = _Recorder(_rerun_handler())
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="single-track production.*--restart"):
        client.rerun_production(stored, _rerun_payload(tmp_path))
    assert recorder.calls == []


def test_rerun_does_not_start_when_the_update_did_not_land(tmp_path: Path) -> None:
    stale = [{"start": "00:00:00", "title": "Old"}, {"start": "00:00:00", "title": "Intro"}]
    recorder = _Recorder(_rerun_handler(chapters_after_update=stale))
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="it was not started"):
        client.rerun_production(_stored(), _rerun_payload(tmp_path))
    assert ("POST", "/api/production/u1/start.json") not in recorder.calls


def test_rerun_start_failure_points_at_update(tmp_path: Path) -> None:
    rerun = _rerun_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start.json"):
            raise httpx.ReadTimeout("timed out", request=request)
        return rerun(request)

    with (
        _client(_Recorder(handler)) as client,
        pytest.raises(AuphonicApiError, match="u1 was updated but starting it failed.*--update"),
    ):
        client.rerun_production(_stored(), _rerun_payload(tmp_path))


def test_rerun_polling_ignores_the_previous_done_until_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = itertools.count()
    monkeypatch.setattr(auphonic_api.time, "monotonic", lambda: float(next(ticks)))
    statuses = [(3, "Done"), (15, "Production Changed"), (4, "Audio Processing"), (3, "Done")]
    recorder = _Recorder(_polling_handler(statuses))
    with _client(recorder) as client:
        production = client.wait_for_production("u1", poll_interval=0, timeout_seconds=60, rerun_grace_seconds=30)
    assert production.status == 3
    assert len(recorder.requests) == 4


def test_rerun_polling_gives_up_on_a_stale_done(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 10.0, 20.0, 40.0])
    monkeypatch.setattr(auphonic_api.time, "monotonic", lambda: next(ticks))
    recorder = _Recorder(_polling_handler([(3, "Done")] * 3))
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="still reports its previous run"):
        client.wait_for_production("u1", poll_interval=0, timeout_seconds=600, rerun_grace_seconds=30)


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
        self.multitrack: dict[str, bool] = {}
        self.preset_multitrack = True
        self.uploads: list[httpx.Request] = []
        self.tracks: dict[str, list[dict[str, Any]]] = {}
        self.chapters: dict[str, list[dict[str, Any]]] = {}
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.track_updates: list[tuple[str, str, dict[str, Any]]] = []
        self.recorder = _Recorder(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/preset/"):
            return _preset(path.removeprefix("/api/preset/").removesuffix(".json"), multitrack=self.preset_multitrack)
        if path == "/api/productions.json":
            uuid = f"prod-{len(self.created) + 1}"
            body = json.loads(request.content)
            self.created.append(uuid)
            self.bodies.append(body)
            self.multitrack[uuid] = body.get("is_multitrack") is True
            self.tracks[uuid] = [dict(track) for track in body.get("multi_input_files", [])]
            self.chapters[uuid] = list(body.get("chapters", []))
            return _ok(_production(uuid, 10, "Production Not Started Yet"))
        if path.endswith("/upload.json"):
            self.uploads.append(request)
            return _ok(_production(path.split("/")[3], 10, "Production Not Started Yet"))
        if path.endswith("/start.json"):
            uuid = path.removeprefix("/api/production/").removesuffix("/start.json")
            self.started.append(uuid)
            if self.start_error is not None:
                raise self.start_error(request)
            # A re-run restarts the script: the stored production runs again.
            self.polls[uuid] = 0
            return _ok(_production(uuid, 1, "Waiting"))
        if "/multi_input_files/" in path and request.method == "POST":
            uuid, _, track_id = (
                path.removeprefix("/api/production/").removesuffix(".json").partition("/multi_input_files/")
            )
            self.track_updates.append((uuid, track_id, json.loads(request.content)))
            return _ok(self._production(uuid, 3, "Done"))
        if path.startswith("/api/production/") and path.endswith(".json") and path.count("/") == 3:
            uuid = path.removeprefix("/api/production/").removesuffix(".json")
            if request.method == "POST":
                body = json.loads(request.content)
                self.updates.append((uuid, body))
                # Re-applying the preset clears the chapters before the request's own are added.
                self.chapters[uuid] = (
                    list(body.get("chapters", []))
                    if "preset" in body
                    else [
                        *self.chapters.get(uuid, []),
                        *body.get("chapters", []),
                    ]
                )
                return _ok(self._production(uuid, 15, "Production Changed"))
            index = self.polls.get(uuid, 0)
            self.polls[uuid] = index + 1
            code, name = self.script[min(index, len(self.script) - 1)]
            return _ok(self._production(uuid, code, name))
        if path.startswith("/download/"):
            return httpx.Response(200, content=b"mp3-bytes")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def _production(self, uuid: str, code: int, name: str) -> dict[str, Any]:
        url = f"https://auphonic.test/download/{uuid}/ep.mp3" if code == 3 else None
        extra: dict[str, Any] = {
            "output_files": [{"format": "mp3", "download_url": url, "filename": "ep.mp3"}],
            "chapters": self.chapters.get(uuid, []),
            "multi_input_files": self.tracks.get(uuid, []),
            "change_allowed": True,
        }
        if uuid in self.multitrack:
            extra["is_multitrack"] = self.multitrack[uuid]
        return _production(uuid, code, name, **extra)


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
        lambda **_kwargs: {
            "preset": "p1",
            "is_multitrack": True,
            "multi_input_files": [{"type": "multitrack", "id": "jochen", "input_file": "https://cdn.example/j.flac"}],
        },
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


def test_run_produce_with_real_payload_builder_uploads_stems(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic],
) -> None:
    monkeypatch.setattr(produce, "build_auphonic_payload", build_auphonic_payload)
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(tmp_path / "missing-config.yaml"))
    fake = fake_api([(3, "Done")])
    workspace = _workspace(tmp_path)
    stems = workspace / "stems"
    stems.mkdir()
    (stems / "jochen.flac").write_bytes(b"fLaC-j")
    (stems / "dominik.flac").write_bytes(b"fLaC-d")
    (workspace / "episode.yaml").write_text(
        "episode_id: ep_001\nauphonic:\n  preset_id: p1\n  tracks:\n"
        "    - id: jochen\n      path: stems/jochen.flac\n"
        "    - id: dominik\n      path: stems/dominik.flac\n",
        encoding="utf-8",
    )

    produce.run_produce(workspace=workspace, dry_run=False)

    assert fake.bodies[0]["is_multitrack"] is True
    assert fake.bodies[0]["output_basename"] == "ep_001"
    assert fake.bodies[0]["multi_input_files"] == [
        {"type": "multitrack", "id": "jochen"},
        {"type": "multitrack", "id": "dominik"},
    ]
    assert "input_file" not in fake.bodies[0]
    assert len(fake.uploads) == 1
    assert b'name="jochen"; filename="jochen.flac"' in fake.uploads[0].content
    assert b'name="dominik"; filename="dominik.flac"' in fake.uploads[0].content
    assert fake.started == ["prod-1"]


def test_run_produce_single_track_opt_in_keeps_url_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic],
) -> None:
    monkeypatch.setattr(produce, "build_auphonic_payload", build_auphonic_payload)
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(tmp_path / "missing-config.yaml"))
    fake = fake_api([(3, "Done")])
    fake.preset_multitrack = False
    workspace = _workspace(tmp_path)
    (workspace / "episode.yaml").write_text(
        "episode_id: ep_001\nauphonic:\n  preset_id: p1\n  mode: single_track\n"
        "  input_file: https://cdn.example/ep.mp3\n",
        encoding="utf-8",
    )

    produce.run_produce(workspace=workspace, dry_run=False)

    assert fake.bodies[0]["input_file"] == "https://cdn.example/ep.mp3"
    assert "is_multitrack" not in fake.bodies[0]
    assert fake.bodies[0]["preset"] == "p1"
    assert fake.started == ["prod-1"]


def test_run_produce_refuses_to_resume_a_stored_single_track_production(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([(3, "Done")])
    fake.multitrack["prod-premix"] = False
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    store.set_auphonic_production_uuid("prod-premix", default=EpisodeWorkspace(episode_id="ep_001", root_dir="."))

    with pytest.raises(typer.BadParameter, match="prod-premix is a single-track production") as excinfo:
        produce.run_produce(workspace=workspace, dry_run=False)
    assert "--restart" in str(excinfo.value)
    assert fake.created == []
    assert store.read_state().auphonic_production_uuid == "prod-premix"

    produce.run_produce(workspace=workspace, dry_run=False, restart=True)
    assert fake.created == ["prod-1"]
    assert fake.bodies[0]["is_multitrack"] is True
    assert store.read_state().auphonic_production_uuid == "prod-1"


def test_run_produce_resumes_a_stored_multitrack_production(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([(4, "Audio Processing"), (3, "Done")])
    workspace = _workspace(tmp_path)

    produce.run_produce(workspace=workspace, dry_run=False)
    produce.run_produce(workspace=workspace, dry_run=False)

    assert fake.created == ["prod-1"]
    assert fake.started == ["prod-1"]


def test_run_produce_update_reuses_the_stored_production_without_uploads(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic],
) -> None:
    fake = fake_api([(3, "Done")])
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    produce.run_produce(workspace=workspace, dry_run=False)
    assert fake.created == ["prod-1"]
    (store.layout.auphonic_outputs_dir / "ep.mp3").write_bytes(b"old")
    capsys.readouterr()

    fake.script = [(4, "Audio Processing"), (3, "Done")]
    produce.run_produce(workspace=workspace, dry_run=False, update=True)

    out = capsys.readouterr().out
    assert "Updating Auphonic production prod-1" in out
    assert "costs Auphonic credits again" in out
    assert fake.created == ["prod-1"]
    assert fake.started == ["prod-1", "prod-1"]
    assert fake.uploads == []
    assert [uuid for uuid, _body in fake.updates] == ["prod-1"]
    assert "multi_input_files" not in fake.updates[0][1]
    assert fake.track_updates == [("prod-1", "jochen", {"type": "multitrack", "id": "jochen", "offset": 0})]
    assert (store.layout.auphonic_outputs_dir / "ep.mp3").read_bytes() == b"mp3-bytes"
    assert store.read_state().auphonic_production_uuid == "prod-1"


def test_run_produce_update_needs_a_stored_production(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([(3, "Done")])
    workspace = _workspace(tmp_path)

    with pytest.raises(typer.BadParameter, match="nothing to update"):
        produce.run_produce(workspace=workspace, dry_run=False, update=True)
    with pytest.raises(typer.BadParameter, match="nothing to update"):
        produce.run_produce(workspace=workspace, dry_run=True, update=True)
    with pytest.raises(typer.BadParameter, match="cannot be combined"):
        produce.run_produce(workspace=workspace, dry_run=False, update=True, restart=True)
    assert fake.recorder.requests == []


def test_run_produce_update_dry_run_prints_the_update_without_api_calls(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic],
) -> None:
    fake = fake_api([(3, "Done")])
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    store.set_auphonic_production_uuid("prod-9", default=EpisodeWorkspace(episode_id="ep_001", root_dir="."))

    produce.run_produce(workspace=workspace, dry_run=True, update=True)

    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "production_uuid": "prod-9",
        "production_update": {"preset": "p1"},
        "track_updates": [{"type": "multitrack", "id": "jochen", "offset": 0}],
    }
    assert fake.recorder.requests == []


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


def test_default_client_keeps_environment_proxy_support(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    with AuphonicClient(CREDS) as client:
        mounts = client._client._mounts
        assert any(pattern.matches(httpx.URL("https://auphonic.com/api")) for pattern in mounts)


class _InterruptedDownload(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b"partial"
        raise httpx.ReadError("stream interrupted")


def test_interrupted_download_cleans_temp_and_preserves_destination(tmp_path: Path) -> None:
    dest = tmp_path / "ep.mp3"
    dest.write_bytes(b"old")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=_InterruptedDownload()))
    with AuphonicClient(CREDS, transport=transport) as client:
        with pytest.raises(AuphonicApiError, match="download") as error:
            client.download_outputs(
                [auphonic_api.AuphonicOutputFile(url=f"{BASE}/audio", filename="ep.mp3")], tmp_path
            )
    assert isinstance(error.value.__cause__, httpx.ReadError)
    assert dest.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [dest]


@pytest.mark.parametrize("operation", ["write", "flush", "fsync", "replace"])
def test_download_disk_failure_cleans_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    dest = tmp_path / "ep.mp3"
    dest.write_bytes(b"old")
    called = False

    def fail(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        called = True
        raise OSError("disk failure")

    if operation in {"fsync", "replace"}:
        monkeypatch.setattr(auphonic_api.os, operation, fail)
    else:
        original = auphonic_api.tempfile.NamedTemporaryFile

        def failing_temp(*args: Any, **kwargs: Any) -> Any:
            handle = original(*args, **kwargs)
            monkeypatch.setattr(handle, operation, fail)
            return handle

        monkeypatch.setattr(auphonic_api.tempfile, "NamedTemporaryFile", failing_temp)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"new"))
    with AuphonicClient(CREDS, transport=transport) as client:
        with pytest.raises(AuphonicApiError, match="download") as error:
            client.download_outputs(
                [auphonic_api.AuphonicOutputFile(url=f"{BASE}/audio", filename="ep.mp3")], tmp_path
            )
    assert called
    assert isinstance(error.value.__cause__, OSError)
    assert dest.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [dest]


def test_successful_download_replaces_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "ep.mp3"
    dest.write_bytes(b"old")
    replacements: list[Path] = []
    original = auphonic_api.os.replace

    def replace(source: Path, target: Path) -> None:
        replacements.append(target)
        original(source, target)

    monkeypatch.setattr(auphonic_api.os, "replace", replace)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"new"))
    with AuphonicClient(CREDS, transport=transport) as client:
        assert client.download_outputs(
            [auphonic_api.AuphonicOutputFile(url=f"{BASE}/audio", filename="ep.mp3")], tmp_path
        ) == (dest,)
    assert replacements == [dest]
    assert dest.read_bytes() == b"new"
    assert list(tmp_path.iterdir()) == [dest]


def test_download_directory_failure_uses_api_error(tmp_path: Path) -> None:
    output_dir = tmp_path / "not_a_directory"
    output_dir.write_bytes(b"existing")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"new"))
    with AuphonicClient(CREDS, transport=transport) as client:
        with pytest.raises(AuphonicApiError, match="download directory") as error:
            client.download_outputs(
                [auphonic_api.AuphonicOutputFile(url=f"{BASE}/audio", filename="ep.mp3")], output_dir
            )
    assert isinstance(error.value.__cause__, OSError)
    assert output_dir.read_bytes() == b"existing"
