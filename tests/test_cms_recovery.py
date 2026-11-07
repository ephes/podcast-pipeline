"""Synthetic recovery-state and success-envelope regressions."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer

from podcast_pipeline.cms_api import CmsApiError, DjangoCastClient
from podcast_pipeline.entrypoints import cms_draft as entry
from tests.test_cms_draft import BASE, PODCAST_ID, TOKEN, FakeCast, _config, _run, _workspace


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeCast:
    server = FakeCast()
    monkeypatch.setattr(
        entry,
        "DjangoCastClient",
        lambda base_url, token: DjangoCastClient(base_url, token, transport=server.transport()),
    )
    return server


@pytest.mark.parametrize(
    "changes",
    [
        {"episode_id": "bad"},
        {"audio_id": False},
        {"episode_id": 0},
        {"audio_attached": 1},
        {"transcript_pending": "true"},
        {"transcript_pending": None},
        {"parent_id": True},
        {"base_url": None},
    ],
)
def test_invalid_recovery_fields_stop_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, changes: dict[str, Any]
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    path = workspace / "cms/draft.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"base_url": BASE, **changes}))
    before = path.read_bytes()
    with pytest.raises(typer.BadParameter, match="recovery state"):
        _run(workspace)
    assert not [r for r in fake.requests if r.method in {"POST", "PATCH"}]
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", [[], None, 4, {}])
def test_invalid_recovery_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, value: Any) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    path = workspace / "cms/draft.json"
    path.parent.mkdir()
    path.write_text(json.dumps(value))
    with pytest.raises(typer.BadParameter, match="recovery state"):
        _run(workspace)
    assert not fake.audios


@pytest.mark.parametrize("attached", [False, True])
def test_parent_change_refuses_all_stored_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, attached: bool
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _run(workspace)
    path = workspace / "cms/draft.json"
    state = json.loads(path.read_text())
    state.update(audio_attached=attached)
    path.write_text(json.dumps(state))
    _config(tmp_path, monkeypatch, parent_id=PODCAST_ID + 1)
    original = fake.handle

    def handle(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path == "/api/editor/parents/":
            return httpx.Response(200, json=[{"id": PODCAST_ID + 1, "type": "cast.Podcast"}])
        return response

    monkeypatch.setattr(fake, "handle", handle)
    before = path.read_bytes()
    fake.requests.clear()
    with pytest.raises(typer.BadParameter, match="podcast.*reset"):
        _run(workspace)
    assert not [r for r in fake.requests if r.method in {"POST", "PATCH"}]
    assert path.read_bytes() == before


@pytest.mark.parametrize("episode_id", [None, 141])
def test_legacy_ambiguous_or_changed_parent_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, episode_id: int | None
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _run(workspace)
    path = workspace / "cms/draft.json"
    state = json.loads(path.read_text())
    state.pop("parent_id", None)
    state["episode_id"] = episode_id
    path.write_text(json.dumps(state))
    fake.episodes[141]["parent"]["id"] = PODCAST_ID + 1
    fake.requests.clear()
    with pytest.raises(typer.BadParameter, match="reset"):
        _run(workspace)
    assert not [r for r in fake.requests if r.method in {"POST", "PATCH"}]


def test_legacy_verified_episode_adopted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _run(workspace)
    path = workspace / "cms/draft.json"
    state = json.loads(path.read_text())
    state.pop("parent_id", None)
    path.write_text(json.dumps(state))
    _run(workspace)
    assert json.loads(path.read_text())["parent_id"] == PODCAST_ID
    assert len(fake.audios) == len(fake.episodes) == 1


@pytest.mark.parametrize("object_name, identity", [("Audio", 83), ("episode", 141)])
def test_checkpoint_failure_reports_created_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, object_name: str, identity: int
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    original = entry._write_state
    denied = []

    def write(store: Any, state: entry.CmsDraftState) -> None:
        if (object_name == "Audio" and state.audio_id) or (object_name == "episode" and state.episode_id):
            denied.append(True)
            raise PermissionError("synthetic denial")
        original(store, state)

    monkeypatch.setattr(entry, "_write_state", write)
    with pytest.raises(typer.BadParameter, match=rf"{object_name} {identity}.*succeeded.*draft.json"):
        _run(workspace)
    assert denied == [True]
    assert len(fake.audios) == 1
    assert len(fake.episodes) == (0 if object_name == "Audio" else 1)
    path = workspace / "cms/draft.json"
    if object_name == "Audio":
        assert not path.exists()
    else:
        assert json.loads(path.read_text())["episode_id"] is None
        assert not [r for r in fake.requests if r.method == "PATCH"]


@pytest.mark.parametrize("method", ["create", "update", "get", "upload", "lookup"])
@pytest.mark.parametrize("invalid", [True, "7", None, 0, -1])
def test_client_rejects_invalid_success_identity(tmp_path: Path, method: str, invalid: Any) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": invalid, "latest_revision_id": 9, "private": "SENTINEL"})

    client = DjangoCastClient(BASE, TOKEN, transport=httpx.MockTransport(handle))
    with pytest.raises(CmsApiError, match="invalid.*id") as exc:
        _call(client, method, tmp_path)
    assert "SENTINEL" not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.parametrize("method", ["create", "update", "get"])
@pytest.mark.parametrize("revision", [None, True, "9", 0])
def test_client_requires_revision(tmp_path: Path, method: str, revision: Any) -> None:
    client = DjangoCastClient(
        BASE,
        TOKEN,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"id": 7, "latest_revision_id": revision})),
    )
    with pytest.raises(CmsApiError, match="latest_revision_id"):
        _call(client, method, tmp_path)


def _call(client: DjangoCastClient, method: str, tmp_path: Path) -> Any:
    from podcast_pipeline.cms_draft import AudioFile, AudioUpload

    if method == "create":
        return client.create_episode({})
    if method == "update":
        return client.update_episode(7, {}, base_revision_id=9)
    if method == "get":
        return client.get_episode(7)
    if method == "lookup":
        return client.find_post(parent_id=120, slug="test")
    path = tmp_path / "test.mp3"
    path.write_bytes(b"synthetic")
    return client.upload_audio(
        AudioUpload(
            file=AudioFile(field="mp3", path=path),
            title="Test",
            subtitle="",
            chaptermarks="",
            collection=None,
            diarization_mode=None,
        )
    )


@pytest.mark.parametrize("foreign", [False, True])
def test_empty_or_foreign_checkpoint_still_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, foreign: bool
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    path = workspace / "cms/draft.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"base_url": "https://other.test/" if foreign else BASE, "episode_id": None, "audio_id": None})
    )
    _run(workspace)
    assert len(fake.episodes) == len(fake.audios) == 1


@pytest.mark.parametrize("method", ["create", "update", "get", "upload", "lookup"])
def test_missing_identity_stops_client(tmp_path: Path, method: str) -> None:
    client = DjangoCastClient(BASE, TOKEN, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(CmsApiError, match="invalid id"):
        _call(client, method, tmp_path)


def test_reset_ids_allows_parent_rebinding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    path = workspace / "cms/draft.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"base_url": BASE, "parent_id": PODCAST_ID + 1, "episode_id": None, "audio_id": None}))
    _run(workspace)
    assert json.loads(path.read_text())["parent_id"] == PODCAST_ID


def test_deleted_bound_episode_falls_back_to_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _run(workspace)
    fake.episodes.clear()
    _run(workspace)
    assert len(fake.audios) == len(fake.episodes) == 1
    assert fake.episodes[142]["podcast_audio"] == {"id": 83}


def test_transcript_checkpoint_identifies_remote_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast
) -> None:
    workspace = _workspace(tmp_path, extra_formats=False)
    _config(tmp_path, monkeypatch)
    fake.accept_enabled_diarization = True
    fake.supports_contributors = True
    fake.options_allow["/api/editor/episodes/141/transcript/"] = "POST"
    original = entry._write_state

    def write(store: Any, state: entry.CmsDraftState) -> None:
        if fake.calls("POST", "/api/editor/episodes/141/transcript/"):
            raise PermissionError("synthetic denial")
        original(store, state)

    monkeypatch.setattr(entry, "_write_state", write)
    with pytest.raises(typer.BadParameter, match="Transcript generation for episode 141 succeeded remotely"):
        _run(workspace)
    assert len(fake.calls("POST", "/api/editor/episodes/141/transcript/")) == 1
    assert json.loads((workspace / "cms/draft.json").read_text())["transcript_pending"] is True
