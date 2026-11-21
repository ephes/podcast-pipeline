from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from starlette.testclient import TestClient

from podcast_pipeline.dashboard_context import DashboardContext
from podcast_pipeline.entrypoints.dashboard_web import create_dashboard_app
from podcast_pipeline.entrypoints.ingest import run_ingest
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore, WorkspaceStoreError
from tests.session_support import SESSION_HEADERS, SESSION_TOKEN


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
@pytest.mark.parametrize("invalid_notes", ["[]", "false", "{description: [unexpected]}"])
def test_asset_notes_refuse_invalid_persisted_map(tmp_path: Path, method: str, invalid_notes: str) -> None:
    ctx = DashboardContext(workspace=tmp_path)
    episode = ctx.layout.episode_yaml
    original = f"# Preserve this comment\nepisode_id: synthetic\ncustom: keep\neditorial_notes: {invalid_notes}\n"
    episode.write_bytes(original.encode())
    with TestClient(
        create_dashboard_app(ctx=ctx, session_token=SESSION_TOKEN),
        base_url="http://127.0.0.1",
        headers=SESSION_HEADERS,
        raise_server_exceptions=False,
    ) as client:
        response = client.request(
            method,
            "/api/assets/description/notes",
            json={"notes": "updated"} if method == "PUT" else None,
            headers={"Content-Type": "application/json"},
        )
    assert episode.read_bytes() == original.encode()
    assert response.status_code == 400
    assert "editorial_notes" in response.json()["error"]


def test_asset_notes_update_preserves_other_entries(tmp_path: Path) -> None:
    ctx = DashboardContext(workspace=tmp_path)
    ctx.store.write_episode_yaml(
        {"episode_id": "synthetic", "custom": "keep", "editorial_notes": {"description": "old", "shownotes": "keep"}}
    )
    with TestClient(
        create_dashboard_app(ctx=ctx, session_token=SESSION_TOKEN),
        base_url="http://127.0.0.1",
        headers=SESSION_HEADERS,
    ) as client:
        response = client.put("/api/assets/description/notes", json={"notes": "updated"})
    assert response.status_code == 200
    saved = ctx.store.read_episode_yaml()
    assert saved["editorial_notes"] == {"description": "updated", "shownotes": "keep"}
    assert saved["custom"] == "keep"


def test_stale_document_write_is_rejected(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep1"})
    stale = store.read_episode_yaml()
    DashboardContext(workspace=tmp_path).set_editorial_notes("description", "new")
    with pytest.raises(WorkspaceStoreError, match="reload"):
        store.write_episode_yaml(stale)
    assert store.read_episode_yaml()["editorial_notes"]["description"] == "new"


@pytest.mark.parametrize("ingest", [False, True])
def test_concurrent_editorial_updates_survive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ingest: bool) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep1"})
    first = DashboardContext(workspace=tmp_path)
    second = DashboardContext(workspace=tmp_path)
    read_started = Event()
    second_finished = Event()
    original = first.store.read_episode_yaml

    def paused_read() -> dict[str, Any]:
        data = original()
        read_started.set()
        second_finished.wait(0.2)
        return data

    monkeypatch.setattr(first.store, "read_episode_yaml", paused_read)
    media = tmp_path / "media"
    media.mkdir()
    (media / "host.flac").write_bytes(b"audio")

    def other_update() -> None:
        assert read_started.wait(2)
        try:
            if ingest:
                run_ingest(workspace=tmp_path, reaper_media_dir=media, tracks_glob="*.flac")
            else:
                second.set_editorial_notes("shownotes", "second")
        finally:
            second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first.set_editorial_notes, "description", "first")
        b = pool.submit(other_update)
        a.result(timeout=5)
        b.result(timeout=5)
    data = store.read_episode_yaml()
    assert data["editorial_notes"]["description"] == "first"
    if ingest:
        assert data["tracks"][0]["path"] == "host.flac"
    else:
        assert data["editorial_notes"]["shownotes"] == "second"


def test_revision_write_and_stale_revision(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep1"})
    data, revision = store.read_episode_yaml_with_revision()
    data["hosts"] = ["Alice"]
    store.write_episode_yaml(data, expected_revision=revision)
    before = store.layout.episode_yaml.read_bytes()
    with pytest.raises(WorkspaceStoreError, match="reload"):
        store.write_episode_yaml(data, expected_revision=revision)
    assert store.layout.episode_yaml.read_bytes() == before


def test_failed_update_preserves_bytes(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep1"})
    before = store.layout.episode_yaml.read_bytes()
    with pytest.raises(RuntimeError, match="abort"):
        with store.update_episode_yaml() as data:
            data["hosts"] = ["Alice"]
            raise RuntimeError("abort")
    assert store.layout.episode_yaml.read_bytes() == before
