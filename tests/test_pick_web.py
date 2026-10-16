from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Generator
from functools import partial
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from podcast_pipeline import asset_refine_llm
from podcast_pipeline.asset_refine_llm import RefineRequest, parse_refined_candidate
from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace, TextFormat
from podcast_pipeline.entrypoints.pick_web import _PickWebHandler, _ServerContext
from podcast_pipeline.pick_core import has_manual_selection_provenance
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
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
    sock.sendall(
        f"POST /api/select HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\n\r\n".encode()
    )
    resp = sock.recv(4096).decode()
    sock.close()
    assert "411" in resp.split("\r\n")[0]


def _post_json(url: str, payload: object) -> tuple[int, dict[str, object]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
    return resp.status, json.loads(resp.read().decode("utf-8"))


def _assets_by_id(base_url: str) -> dict[str, dict[str, object]]:
    resp = urllib.request.urlopen(f"{base_url}/api/assets")
    return {a["asset_id"]: a for a in json.loads(resp.read().decode("utf-8"))}


def test_post_api_select_custom_saves_edited_text_and_counts_as_selected(
    pick_server: _PickServerTuple,
) -> None:
    _server, base_url, ctx, candidates_by_asset = pick_server
    candidate = candidates_by_asset["description"][1]

    before = _assets_by_id(base_url)["description"]
    assert before["has_selection"] is False
    assert before["custom_selected"] is False
    assert before["selection_format"] == "markdown"

    status, data = _post_json(
        f"{base_url}/api/select-custom",
        {
            "asset_id": "description",
            "text": "# Description 2\n\nSecond candidate, edited.\n",
            "based_on_candidate_id": str(candidate.candidate_id),
        },
    )
    assert status == 200
    assert data == {"ok": True, "text": "# Description 2\n\nSecond candidate, edited.\n"}

    layout = ctx.store.layout
    assert layout.selected_text_path("description", TextFormat.markdown).read_text(encoding="utf-8") == (
        "# Description 2\n\nSecond candidate, edited.\n"
    )
    assert "<h1>Description 2</h1>" in layout.selected_text_path("description", TextFormat.html).read_text(
        encoding="utf-8"
    )

    after = _assets_by_id(base_url)["description"]
    assert after["has_selection"] is True
    assert after["custom_selected"] is True
    assert after["selected_candidate_id"] is None
    assert after["selected_text"] == "# Description 2\n\nSecond candidate, edited.\n"

    state = ctx.store.read_state()
    asset = next(a for a in state.assets if a.asset_id == "description")
    assert asset.selected_candidate_id is None
    assert has_manual_selection_provenance(state, "description")

    # Picking a candidate afterwards replaces the custom selection and its marker.
    status, _ = _post_json(
        f"{base_url}/api/select",
        {"asset_id": "description", "candidate_id": str(candidate.candidate_id)},
    )
    assert status == 200
    picked = _assets_by_id(base_url)["description"]
    assert picked["custom_selected"] is False
    assert picked["selected_candidate_id"] == str(candidate.candidate_id)
    assert not has_manual_selection_provenance(ctx.store.read_state(), "description")


def test_post_api_select_custom_validates_slug(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    slug_candidate = Candidate(asset_id="slug", content="python-neuigkeiten")
    store.write_candidate(slug_candidate)
    ctx = _ServerContext(
        store=store,
        candidates_by_asset={"slug": [slug_candidate]},
        workspace_state=EpisodeWorkspace(episode_id="test_ep", root_dir="."),
    )

    error = ctx.select_custom_text("slug", "Python 3.15", None)
    assert error is not None
    assert "Invalid slug" in error
    assert "python-3-15" in error
    assert not store.layout.copy_selected_dir.exists() or not any(store.layout.copy_selected_dir.iterdir())

    assert ctx.select_custom_text("slug", "python-315", str(slug_candidate.candidate_id)) is None
    assert store.layout.selected_text_path("slug", TextFormat.plain).read_text(encoding="utf-8") == "python-315\n"
    assets = {a["asset_id"]: a for a in ctx.get_assets_json()}
    assert assets["slug"]["selection_format"] == "plain"
    assert assets["slug"]["has_selection"] is True


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"asset_id": "description"}, "Missing asset_id or text"),
        ({"asset_id": "description", "text": "x", "based_on_candidate_id": 1}, "must be a string"),
        ({"asset_id": "description", "text": "x", "based_on_candidate_id": "nope"}, "Invalid based_on_candidate_id"),
        ({"asset_id": "unknown", "text": "x"}, "Unknown asset_id"),
        ({"asset_id": "description", "text": "  \n"}, "must not be empty"),
        (["not", "an", "object"], "Expected JSON object"),
    ],
)
def test_post_api_select_custom_rejects_bad_input(
    pick_server: _PickServerTuple,
    payload: object,
    message: str,
) -> None:
    _server, base_url, ctx, _candidates = pick_server
    status, data = _post_json(f"{base_url}/api/select-custom", payload)
    assert status == 400
    assert message in str(data["error"])
    assert not ctx.store.layout.state_json.exists()


def _fake_refine(calls: list[RefineRequest]) -> Any:
    def refine(*, workspace: Path, request: RefineRequest) -> Candidate:
        calls.append(request)
        return parse_refined_candidate(
            {"content": f"# Kürzer\n\n{request.source_text.splitlines()[-1]}"},
            asset_id=request.asset_id,
            instruction=request.instruction,
            based_on_candidate_id=request.based_on_candidate_id,
        )

    return refine


def test_post_api_refine_adds_refined_candidate(
    pick_server: _PickServerTuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _server, base_url, ctx, candidates_by_asset = pick_server
    with ctx.store.update_episode_yaml() as episode:
        episode["editorial_notes"] = {"description": "Duzen"}
    calls: list[RefineRequest] = []
    monkeypatch.setattr(asset_refine_llm, "refine_asset_text", _fake_refine(calls))
    source = candidates_by_asset["description"][0]

    status, data = _post_json(
        f"{base_url}/api/refine",
        {"asset_id": "description", "instruction": "kürzer", "candidate_id": str(source.candidate_id)},
    )

    assert status == 200, data
    refined = cast(dict[str, Any], data["candidate"])
    assert refined["instruction"] == "kürzer"
    assert refined["content"] == "# Kürzer\n\nFirst candidate."
    assert calls[0].based_on_candidate_id == source.candidate_id
    assert calls[0].editorial_notes == "Duzen"
    assert ctx.store.layout.candidate_json_path("description", UUID(refined["candidate_id"])).exists()

    asset = _assets_by_id(base_url)["description"]
    candidates = cast(list[dict[str, Any]], asset["candidates"])
    assert len(candidates) == 3
    assert candidates[-1]["instruction"] == "kürzer"
    assert candidates[0]["instruction"] is None

    # The refined candidate can be selected like any other.
    status, _ = _post_json(
        f"{base_url}/api/select", {"asset_id": "description", "candidate_id": refined["candidate_id"]}
    )
    assert status == 200


def test_post_api_refine_editor_text(pick_server: _PickServerTuple, monkeypatch: pytest.MonkeyPatch) -> None:
    _server, base_url, _ctx, _candidates = pick_server
    calls: list[RefineRequest] = []
    monkeypatch.setattr(asset_refine_llm, "refine_asset_text", _fake_refine(calls))

    status, _ = _post_json(
        f"{base_url}/api/refine",
        {"asset_id": "shownotes", "instruction": "ohne Emojis", "text": "Meine Notizen 🎉"},
    )

    assert status == 200
    assert calls[0].source_text == "Meine Notizen 🎉"
    assert calls[0].based_on_candidate_id is None


def test_post_api_refine_reports_drafter_failure(
    pick_server: _PickServerTuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _server, base_url, ctx, _candidates = pick_server

    def failing(*, workspace: Path, request: RefineRequest) -> Candidate:
        raise RuntimeError("Drafter CLI timed out")

    monkeypatch.setattr(asset_refine_llm, "refine_asset_text", failing)
    status, data = _post_json(f"{base_url}/api/refine", {"asset_id": "description", "instruction": "x", "text": "y"})
    assert status == 502
    assert "Drafter CLI timed out" in str(data["error"])
    assert ctx.refining == set()


def test_post_api_refine_rejects_concurrent_refine_of_same_asset(
    pick_server: _PickServerTuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _server, base_url, _ctx, _candidates = pick_server
    started = threading.Event()
    release = threading.Event()
    calls: list[RefineRequest] = []
    fake = _fake_refine(calls)

    def slow(*, workspace: Path, request: RefineRequest) -> Candidate:
        started.set()
        release.wait(timeout=5)
        return cast(Candidate, fake(workspace=workspace, request=request))

    monkeypatch.setattr(asset_refine_llm, "refine_asset_text", slow)
    results: list[int] = []
    body = {"asset_id": "description", "instruction": "x", "text": "y"}
    first = threading.Thread(target=lambda: results.append(_post_json(f"{base_url}/api/refine", body)[0]))
    first.start()
    assert started.wait(timeout=5)

    # The UI stays responsive while the drafter runs, but a second refine of the same asset is refused.
    assert urllib.request.urlopen(f"{base_url}/api/assets", timeout=5).status == 200
    assert _post_json(f"{base_url}/api/refine", body)[0] == 409

    release.set()
    first.join(timeout=5)
    assert results == [200]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"asset_id": "description"}, "Missing asset_id or instruction"),
        ({"asset_id": "description", "instruction": "x", "text": 1}, "must be strings"),
        ({"asset_id": "description", "instruction": " ", "text": "y"}, "instruction must not be empty"),
        ({"asset_id": "unknown", "instruction": "x", "text": "y"}, "Unknown asset_id"),
        ({"asset_id": "description", "instruction": "x"}, "no text to refine"),
    ],
)
def test_post_api_refine_rejects_bad_input(
    pick_server: _PickServerTuple,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    message: str,
) -> None:
    _server, base_url, _ctx, _candidates = pick_server
    calls: list[RefineRequest] = []
    monkeypatch.setattr(asset_refine_llm, "refine_asset_text", _fake_refine(calls))
    status, data = _post_json(f"{base_url}/api/refine", payload)
    assert status == 400
    assert message in str(data["error"])
    assert calls == []
