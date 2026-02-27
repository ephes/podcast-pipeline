from __future__ import annotations

import json
import threading
import urllib.request
from collections.abc import Generator
from functools import partial
from http.server import HTTPServer
from pathlib import Path

import pytest

from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace
from podcast_pipeline.entrypoints.pick_web import _PickWebHandler, _ServerContext
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def _setup_workspace(tmp_path: Path) -> tuple[EpisodeWorkspaceStore, dict[str, list[Candidate]]]:
    """Create a workspace with candidates for testing."""
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "test_ep"})

    c1 = Candidate(asset_id="description", content="# Description 1\n\nFirst candidate.")
    c2 = Candidate(asset_id="description", content="# Description 2\n\nSecond candidate.")
    c3 = Candidate(asset_id="shownotes", content="# Shownotes\n\nOnly candidate.")

    store.write_candidate(c1)
    store.write_candidate(c2)
    store.write_candidate(c3)

    candidates_by_asset = {
        "description": [c1, c2],
        "shownotes": [c3],
    }
    return store, candidates_by_asset


_PickServerTuple = tuple[HTTPServer, str, _ServerContext, dict[str, list[Candidate]]]


@pytest.fixture()
def pick_server(
    tmp_path: Path,
) -> Generator[_PickServerTuple, None, None]:
    """Start a pick web server on a random port and return (server, base_url, ctx, candidates)."""
    store, candidates_by_asset = _setup_workspace(tmp_path)
    workspace_state = EpisodeWorkspace(episode_id="test_ep", root_dir=".")

    ctx = _ServerContext(
        store=store,
        candidates_by_asset=candidates_by_asset,
        workspace_state=workspace_state,
    )

    handler = partial(_PickWebHandler, ctx)
    server = HTTPServer(("127.0.0.1", 0), handler)
    host = str(server.server_address[0])
    port = int(server.server_address[1])
    base_url = f"http://{host}:{port}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield server, base_url, ctx, candidates_by_asset

    server.shutdown()
    server.server_close()


def test_get_root_returns_html(
    pick_server: _PickServerTuple,
) -> None:
    _server, base_url, _ctx, _candidates = pick_server
    resp = urllib.request.urlopen(f"{base_url}/")
    assert resp.status == 200
    body = resp.read().decode("utf-8")
    assert "<html" in body
    assert "Pick Candidates" in body


def test_get_api_assets_returns_correct_structure(
    pick_server: _PickServerTuple,
) -> None:
    _server, base_url, _ctx, candidates_by_asset = pick_server
    resp = urllib.request.urlopen(f"{base_url}/api/assets")
    assert resp.status == 200
    data = json.loads(resp.read().decode("utf-8"))

    assert isinstance(data, list)
    assert len(data) == 2  # description and shownotes

    asset_ids = {a["asset_id"] for a in data}
    assert asset_ids == {"description", "shownotes"}

    desc_asset = next(a for a in data if a["asset_id"] == "description")
    assert len(desc_asset["candidates"]) == 2
    assert desc_asset["selected_candidate_id"] is None

    for c in desc_asset["candidates"]:
        assert "candidate_id" in c
        assert "content" in c
        assert "content_html" in c
        assert "format" in c


def test_post_api_select_writes_selection(
    pick_server: _PickServerTuple,
) -> None:
    _server, base_url, _ctx, candidates_by_asset = pick_server
    candidate = candidates_by_asset["description"][0]

    payload = json.dumps(
        {
            "asset_id": "description",
            "candidate_id": str(candidate.candidate_id),
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/api/select",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200
    data = json.loads(resp.read().decode("utf-8"))
    assert data["ok"] is True

    # Verify the selection was persisted
    resp2 = urllib.request.urlopen(f"{base_url}/api/assets")
    assets_data = json.loads(resp2.read().decode("utf-8"))
    desc_asset = next(a for a in assets_data if a["asset_id"] == "description")
    assert desc_asset["selected_candidate_id"] == str(candidate.candidate_id)


def test_post_api_select_invalid_candidate_returns_400(
    pick_server: _PickServerTuple,
) -> None:
    _server, base_url, _ctx, _candidates = pick_server

    payload = json.dumps(
        {
            "asset_id": "description",
            "candidate_id": "00000000-0000-0000-0000-000000000000",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/api/select",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        pytest.fail("Expected HTTP 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
        data = json.loads(exc.read().decode("utf-8"))
        assert "error" in data


def test_post_api_select_missing_fields_returns_400(
    pick_server: _PickServerTuple,
) -> None:
    _server, base_url, _ctx, _candidates = pick_server

    payload = json.dumps({"asset_id": "description"}).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/api/select",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        pytest.fail("Expected HTTP 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_run_pick_web_opens_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify run_pick_web opens a browser and serves the UI."""
    store, _candidates = _setup_workspace(tmp_path)

    opened_urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened_urls.append(url))

    # Patch HTTPServer.serve_forever to stop immediately
    from podcast_pipeline.entrypoints import pick_web

    original_serve_forever = HTTPServer.serve_forever

    def fake_serve_forever(self: HTTPServer) -> None:
        # Just shut down immediately
        threading.Thread(target=self.shutdown, daemon=True).start()
        original_serve_forever(self)

    monkeypatch.setattr(HTTPServer, "serve_forever", fake_serve_forever)

    pick_web.run_pick_web(workspace=tmp_path, asset_id=None)

    assert len(opened_urls) == 1
    assert opened_urls[0].startswith("http://127.0.0.1:")


def test_post_api_select_invalid_json_returns_400(
    pick_server: _PickServerTuple,
) -> None:
    _server, base_url, _ctx, _candidates = pick_server

    req = urllib.request.Request(
        f"{base_url}/api/select",
        data=b"not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        pytest.fail("Expected HTTP 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
        data = json.loads(exc.read().decode("utf-8"))
        assert "error" in data


def test_get_unknown_route_returns_404(
    pick_server: _PickServerTuple,
) -> None:
    _server, base_url, _ctx, _candidates = pick_server

    try:
        urllib.request.urlopen(f"{base_url}/nonexistent")
        pytest.fail("Expected HTTP 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_post_unknown_route_returns_404(
    pick_server: _PickServerTuple,
) -> None:
    _server, base_url, _ctx, _candidates = pick_server

    req = urllib.request.Request(
        f"{base_url}/nonexistent",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        pytest.fail("Expected HTTP 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_run_pick_web_exits_on_missing_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_pick_web raises SystemExit when candidates dir is missing."""
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "test_ep"})
    monkeypatch.setattr("webbrowser.open", lambda url: None)

    with pytest.raises(SystemExit):
        from podcast_pipeline.entrypoints.pick_web import run_pick_web

        run_pick_web(workspace=tmp_path, asset_id=None)


def test_run_pick_web_exits_on_empty_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_pick_web raises SystemExit when candidate dirs exist but contain no JSON files."""
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "test_ep"})
    # Create candidate dir structure but no files
    (store.layout.copy_candidates_dir / "description").mkdir(parents=True)
    monkeypatch.setattr("webbrowser.open", lambda url: None)

    with pytest.raises(SystemExit):
        from podcast_pipeline.entrypoints.pick_web import run_pick_web

        run_pick_web(workspace=tmp_path, asset_id="description")


def test_post_api_select_without_content_length(
    pick_server: _PickServerTuple,
) -> None:
    """POST without Content-Length returns 411."""
    _server, base_url, _ctx, _candidates = pick_server

    import socket

    host, port = base_url.replace("http://", "").split(":")
    sock = socket.create_connection((host, int(port)))
    sock.sendall(b"POST /api/select HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    resp = sock.recv(4096).decode()
    sock.close()
    assert "411" in resp.split("\r\n")[0]
