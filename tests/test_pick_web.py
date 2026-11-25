from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from podcast_pipeline import asset_refine_llm
from podcast_pipeline.asset_refine_llm import RefineRequest, parse_refined_candidate
from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace, TextFormat
from podcast_pipeline.entrypoints import pick_web
from podcast_pipeline.entrypoints.pick_web import _PickWebHandler, _ServerContext
from podcast_pipeline.local_web_guard import SESSION_HEADER
from podcast_pipeline.pick_core import has_manual_selection_provenance
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore
from tests.session_support import SESSION_TOKEN, launch_target


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
        session_token=SESSION_TOKEN,
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
    from podcast_pipeline.entrypoints import pick_web

    def open_browser(launch_uri: str) -> None:
        url = launch_target(launch_uri)
        opened_urls.append(url)
        base, _, query = url.partition("?token=")
        threading.Thread(target=lambda: _post_json(f"{base}api/done", {}, session_token=query)).start()

    monkeypatch.setattr("webbrowser.open", open_browser)

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
        f"POST /api/select HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\n"
        f"{SESSION_HEADER}: {SESSION_TOKEN}\r\n\r\n".encode()
    )
    resp = sock.recv(4096).decode()
    sock.close()
    assert "411" in resp.split("\r\n")[0]


@pytest.fixture()
def pick_server_with_tags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[_PickServerTuple, None, None]:
    config = tmp_path / "config.yaml"
    config.write_text("tags:\n  max: 3\n", encoding="utf-8")
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(config))
    store, candidates_by_asset = _setup_workspace(tmp_path)
    with store.update_episode_yaml() as data:
        data["hosts"] = ["Jochen"]
    tag_candidates = [
        Candidate(asset_id="itunes_keywords", content="python, Podcast, jit, Jochen"),
        Candidate(asset_id="itunes_keywords", content="jit, pypy, python"),
    ]
    for candidate in tag_candidates:
        store.write_candidate(candidate)
    candidates_by_asset["itunes_keywords"] = tag_candidates

    ctx = _ServerContext(
        store=store,
        candidates_by_asset=candidates_by_asset,
        workspace_state=EpisodeWorkspace(episode_id="test_ep", root_dir="."),
        session_token=SESSION_TOKEN,
    )
    server = HTTPServer(("127.0.0.1", 0), partial(_PickWebHandler, ctx))
    base_url = f"http://127.0.0.1:{int(server.server_address[1])}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield server, base_url, ctx, candidates_by_asset

    server.shutdown()
    server.server_close()


def _post_tags(base_url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    req = urllib.request.Request(
        f"{base_url}/api/tags",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
    return resp.status, json.loads(resp.read().decode("utf-8"))


def test_get_api_assets_includes_tag_pool(pick_server_with_tags: _PickServerTuple) -> None:
    _server, base_url, _ctx, _candidates = pick_server_with_tags
    assets = json.loads(urllib.request.urlopen(f"{base_url}/api/assets").read().decode("utf-8"))
    keywords = next(a for a in assets if a["asset_id"] == "itunes_keywords")
    assert keywords["is_tag_asset"] is True
    assert keywords["max_tags"] == 3
    assert keywords["selected_tags"] == []
    assert keywords["tag_pool"] == [
        {"tag": "python", "count": 2},
        {"tag": "jit", "count": 2},
        {"tag": "pypy", "count": 1},
    ]
    description = next(a for a in assets if a["asset_id"] == "description")
    assert description["is_tag_asset"] is False
    assert "tag_pool" not in description


def test_post_api_tags_writes_selection(pick_server_with_tags: _PickServerTuple) -> None:
    _server, base_url, ctx, _candidates = pick_server_with_tags
    status, data = _post_tags(base_url, {"asset_id": "itunes_keywords", "tags": ["jit", "JIT", "pypy"]})
    assert status == 200
    assert data["tags"] == ["jit", "pypy"]
    selected = ctx.store.read_selected_text("itunes_keywords", TextFormat.markdown)
    assert selected == "# iTunes keywords\n\n- jit\n- pypy\n"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"asset_id": "itunes_keywords", "tags": ["a", "b", "c", "d"]}, "At most 3"),
        ({"asset_id": "description", "tags": ["a"]}, "does not support"),
        ({"asset_id": "itunes_keywords", "tags": "a"}, "Missing asset_id or tags"),
    ],
)
def test_post_api_tags_rejects_invalid_requests(
    pick_server_with_tags: _PickServerTuple,
    payload: dict[str, object],
    message: str,
) -> None:
    _server, base_url, ctx, _candidates = pick_server_with_tags
    status, data = _post_tags(base_url, payload)
    assert status == 400
    assert message in str(data["error"])
    assert not ctx.store.layout.selected_text_path("itunes_keywords", TextFormat.markdown).exists()


def test_pick_html_does_not_require_tag_assets_for_done(pick_server_with_tags: _PickServerTuple) -> None:
    _server, base_url, _ctx, _candidates = pick_server_with_tags
    body = urllib.request.urlopen(f"{base_url}/").read().decode("utf-8")
    assert "assets.filter(a => !a.is_tag_asset)" in body
    assert "/api/tags" in body


def test_post_api_select_tag_candidate_enforces_cap(pick_server_with_tags: _PickServerTuple) -> None:
    _server, base_url, ctx, candidates = pick_server_with_tags
    candidate = candidates["itunes_keywords"][0]  # python, Podcast, jit, Jochen -> 4 entries > max 3
    req = urllib.request.Request(
        f"{base_url}/api/select",
        data=json.dumps({"asset_id": "itunes_keywords", "candidate_id": str(candidate.candidate_id)}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400
    assert "At most 3" in json.loads(exc_info.value.read().decode("utf-8"))["error"]
    assert not ctx.store.layout.selected_text_path("itunes_keywords", TextFormat.markdown).exists()


def test_pick_html_serializes_tag_saves(pick_server_with_tags: _PickServerTuple) -> None:
    _server, base_url, _ctx, _candidates = pick_server_with_tags
    body = urllib.request.urlopen(f"{base_url}/").read().decode("utf-8")
    assert "function updateTags(asset, compute)" in body
    assert "compute(asset.selected_tags || [])" in body


def _post_json(url: str, payload: object, *, session_token: str | None = None) -> tuple[int, dict[str, object]]:
    headers = {"Content-Type": "application/json"}
    if session_token is not None:
        headers[SESSION_HEADER] = session_token
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
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
        session_token=SESSION_TOKEN,
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
        (["not", "an", "object"], "Invalid JSON object"),
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
    # The page appends the refine receipt; its digest names the same copy a reload shows.
    assert candidates[-1]["candidate_digest"] == refined["candidate_digest"]

    # The refined candidate can be selected like any other.
    status, _ = _post_json(
        f"{base_url}/api/select",
        {
            "asset_id": "description",
            "candidate_id": refined["candidate_id"],
            "candidate_digest": refined["candidate_digest"],
        },
    )
    assert status == 200


def test_refine_acknowledges_published_candidate_with_plain_preview(
    pick_server: _PickServerTuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, url, ctx, candidates = pick_server
    calls: list[RefineRequest] = []
    monkeypatch.setattr(asset_refine_llm, "refine_asset_text", _fake_refine(calls))
    render = pick_web.markdown_to_deterministic_html
    initial_count = len(candidates["description"])
    previewed_after_publication = []

    def fail_refined_preview(content: str) -> str:
        if content.startswith("# Kürzer"):
            paths = ctx.store.layout.copy_candidates_dir.joinpath("description").glob("candidate_*.json")
            previewed_after_publication.append(len(list(paths)) > initial_count)
            raise RuntimeError("Synthetic preview failure")
        return render(content)

    monkeypatch.setattr(pick_web, "markdown_to_deterministic_html", fail_refined_preview)

    status, receipt = _post_json(
        f"{url}/api/refine",
        {"asset_id": "description", "instruction": "kürzer", "text": "A <literal> & character."},
    )

    assert status == 200, receipt
    assert receipt["ok"] is True
    refined = cast(dict[str, str], receipt["candidate"])
    assert refined["content"] == "# Kürzer\n\nA <literal> & character."
    assert refined["content_html"] == "<pre># Kürzer\n\nA &lt;literal&gt; &amp; character.</pre>"
    assert refined["format"] == "markdown"
    assert refined["instruction"] == "kürzer"
    assert previewed_after_publication == [False]
    candidate = ctx.store.read_candidate("description", UUID(refined["candidate_id"]))
    assert candidate.content == refined["content"]
    refreshed = cast(list[dict[str, str]], _assets_by_id(url)["description"]["candidates"])
    assert len(refreshed) == 3
    assert refined in refreshed
    assert len(calls) == 1


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
    assert data["code"] == "refinement_failed"
    assert "Drafter CLI timed out" not in str(data["error"])
    assert ctx.refining == set()


def test_post_api_refine_reports_oversized_prompt_as_input_error(
    pick_server: _PickServerTuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _server, base_url, ctx, _candidates = pick_server
    message = (
        f"Refinement prompt exceeds {asset_refine_llm.MAX_REFINE_PROMPT_BYTES} UTF-8 bytes; "
        "shorten the source text, editorial notes or episode summary and retry."
    )

    def too_large(*, workspace: Path, request: RefineRequest) -> Candidate:
        raise asset_refine_llm.RefinePromptTooLargeError(message)

    monkeypatch.setattr(asset_refine_llm, "refine_asset_text", too_large)
    status, data = _post_json(f"{base_url}/api/refine", {"asset_id": "description", "instruction": "x", "text": "y"})
    assert status == 400
    assert data == {"error": message, "code": "refine_prompt_too_large"}
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


def test_post_api_select_custom_rejects_tag_assets(pick_server_with_tags: _PickServerTuple) -> None:
    _server, base_url, ctx, _candidates = pick_server_with_tags
    req = urllib.request.Request(
        f"{base_url}/api/select-custom",
        data=json.dumps({"asset_id": "itunes_keywords", "text": "a, b, c, d, e"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400
    assert "tag editor" in json.loads(exc_info.value.read().decode("utf-8"))["error"]
    assert not ctx.store.layout.selected_text_path("itunes_keywords", TextFormat.markdown).exists()


def test_pick_html_renders_tag_assets_without_custom_card(pick_server_with_tags: _PickServerTuple) -> None:
    _server, base_url, _ctx, _candidates = pick_server_with_tags
    body = urllib.request.urlopen(f"{base_url}/").read().decode("utf-8")
    tag_branch = "if (asset.is_tag_asset) { renderTagMain(main, asset); return; }"
    assert tag_branch in body
    assert body.index(tag_branch) < body.index("html += renderCustomCard(asset);")


@pytest.mark.parametrize("framing", ["negative", "huge", "duplicate", "conflicting", "transfer", "signed", "eof"])
def test_body_framing_refused(pick_server: _PickServerTuple, framing: str) -> None:
    import socket

    server, _, ctx, candidates = pick_server
    body = json.dumps(
        {"asset_id": "description", "candidate_id": str(candidates["description"][0].candidate_id)}
    ).encode()
    length = len(body)
    headers = {
        "negative": "Content-Length: -1",
        "huge": "Content-Length: 999999999",
        "duplicate": f"Content-Length: {length}\r\nContent-Length: {length}",
        "conflicting": f"Content-Length: {length}\r\nContent-Length: {length + 1}",
        "transfer": f"Transfer-Encoding: chunked\r\nContent-Length: {length}",
        "signed": f"Content-Length: +{length}",
        "eof": f"Content-Length: {length + 1}",
    }
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as sock:
        sock.sendall(
            f"POST /api/select HTTP/1.0\r\nHost: 127.0.0.1:{server.server_port}\r\n"
            f"{SESSION_HEADER}: {SESSION_TOKEN}\r\n"
            f"Content-Type: application/json\r\n{headers[framing]}\r\n\r\n".encode()
            + body
        )
        sock.shutdown(socket.SHUT_WR)
        response = sock.recv(4096)
    assert response.split()[1] in (b"400", b"413")
    assert not ctx.store.layout.state_json.exists()


def test_handler_capacity_and_absolute_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    import time

    from podcast_pipeline.entrypoints import pick_web

    monkeypatch.setattr(pick_web, "_REQUEST_TIMEOUT_SECONDS", 0.2, raising=False)
    server_class = getattr(pick_web, "_PickHTTPServer", ThreadingHTTPServer)
    store, candidates = _setup_workspace(tmp_path)
    ctx = _ServerContext(
        store=store,
        candidates_by_asset=candidates,
        workspace_state=EpisodeWorkspace(episode_id="test_ep", root_dir="."),
        session_token=SESSION_TOKEN,
    )
    server = server_class(("127.0.0.1", 0), partial(_PickWebHandler, ctx))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    sockets = []
    try:
        for _ in range(8):
            sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
            sock.sendall(b"P")
            sockets.append(sock)
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as busy:
            assert b"503" in busy.recv(4096)
        time.sleep(0.4)
        for sock in sockets:
            while sock.recv(4096):
                pass  # Timeout may send 400, but always reaches EOF within the read deadline.
        assert urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/assets").status == 200
    finally:
        for sock in sockets:
            sock.close()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("endpoint", ["select", "tags", "select-custom", "refine", "done"])
@pytest.mark.parametrize(
    "body",
    [
        b"\xff",
        b"null",
        b"[]",
        b'{"asset_id":"x","asset_id":"y"}',
        b'{"x":' + b"[" * 1000 + b"0" + b"]" * 1000 + b"}",
        b'{"x":' + b"1" * 5000 + b"}",
    ],
)
def test_json_control_admission(pick_server: _PickServerTuple, endpoint: str, body: bytes) -> None:
    _, url, ctx, _ = pick_server
    req = urllib.request.Request(f"{url}/api/{endpoint}", data=body, headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(req)
    assert error.value.code == 400
    assert json.loads(error.value.read()) == {"error": "Invalid JSON object"}
    assert not ctx.store.layout.state_json.exists()


def test_assets_release_workspace_snapshot_before_rendering(
    pick_server: _PickServerTuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, url, ctx, candidates = pick_server
    other = _ServerContext(
        store=EpisodeWorkspaceStore(ctx.store.layout.root),
        candidates_by_asset=candidates,
        workspace_state=ctx.workspace_state,
        session_token=SESSION_TOKEN,
    )
    assert other.select_custom_text("description", "Original selection", None) is None
    started, release = threading.Event(), threading.Event()
    render = pick_web.markdown_to_deterministic_html

    def render_paused(content: str) -> str:
        started.set()
        assert release.wait(10)
        return render(content)

    monkeypatch.setattr(pick_web, "markdown_to_deterministic_html", render_paused)

    def update() -> None:
        replacement = candidates["description"][0].model_copy(update={"content": "New candidate content"})
        other.store.write_candidate(replacement)
        assert other.select_custom_text("description", "New selection", None) is None

    with ThreadPoolExecutor(max_workers=2) as executor:
        response = executor.submit(_assets_by_id, url)
        try:
            assert started.wait(5)
            executor.submit(update).result(timeout=3)
        finally:
            release.set()
        snapshot = response.result(timeout=5)["description"]

    assert snapshot["custom_selected"] is True
    assert snapshot["selected_candidate_id"] is None
    assert str(snapshot["selected_text"]).strip() == "Original selection"
    first = cast(list[dict[str, str]], snapshot["candidates"])[0]
    assert first["content"] == candidates["description"][0].content
    assert first["content_html"] == render(first["content"])


def test_assets_refresh_external_selection_and_candidates(pick_server: _PickServerTuple) -> None:
    _, url, ctx, candidates = pick_server
    other = _ServerContext(
        store=EpisodeWorkspaceStore(ctx.store.layout.root),
        candidates_by_asset=candidates,
        workspace_state=ctx.workspace_state,
        session_token=SESSION_TOKEN,
    )
    assert other.select_candidate("description", str(candidates["description"][1].candidate_id)) is None
    added = Candidate(asset_id="description", content="New external candidate")
    other.add_candidate(added)
    data = json.loads(urllib.request.urlopen(f"{url}/api/assets").read())
    desc = next(a for a in data if a["asset_id"] == "description")
    assert desc["selected_candidate_id"] == str(candidates["description"][1].candidate_id)
    assert len(desc["candidates"]) == 3
    assert other.select_custom_text("description", "New custom selection", None) is None
    data = json.loads(urllib.request.urlopen(f"{url}/api/assets").read())
    desc = next(a for a in data if a["asset_id"] == "description")
    assert desc["selected_candidate_id"] is None
    assert desc["selected_text"].strip() == "New custom selection"


@pytest.mark.parametrize("refresh", ["replace", "retire", "republish_during_selection"])
def test_select_refuses_candidate_changed_after_display(
    pick_server: _PickServerTuple, monkeypatch: pytest.MonkeyPatch, refresh: str
) -> None:
    _, url, ctx, candidates = pick_server
    other = EpisodeWorkspaceStore(ctx.store.layout.root)
    candidate = candidates["description"][0]
    assert ctx.select_custom_text("description", "Keep this selection", None) is None
    before = other.layout.state_json.read_bytes()
    displayed = cast(list[dict[str, str]], _assets_by_id(url)["description"]["candidates"])
    assert displayed[0]["candidate_id"] == str(candidate.candidate_id)
    replacement = candidate

    if refresh == "replace":
        replacement = candidate.model_copy(update={"content": "Updated by another producer"})
        other.write_candidate(replacement)
    elif refresh == "retire":
        other.delete_candidate_files("description", candidate.candidate_id)
    else:
        save = pick_web.save_candidate_selection

        def republish_then_save(**kwargs: Any) -> EpisodeWorkspace:
            other.write_candidate(candidate)
            return save(**kwargs)

        monkeypatch.setattr(pick_web, "save_candidate_selection", republish_then_save)

    status, receipt = _post_json(
        f"{url}/api/select", {"asset_id": "description", "candidate_id": str(candidate.candidate_id)}
    )

    assert status == 409, receipt
    assert "reload candidates" in str(receipt["error"])
    assert other.layout.state_json.read_bytes() == before
    assert other.read_selected_text("description", TextFormat.markdown).strip() == "Keep this selection"
    if refresh == "retire":
        assert not other.layout.candidate_json_path("description", candidate.candidate_id).exists()
    else:
        assert other.read_candidate("description", candidate.candidate_id) == replacement


def test_select_refuses_copy_reloaded_by_another_view(pick_server: _PickServerTuple) -> None:
    _, url, ctx, candidates = pick_server
    other = EpisodeWorkspaceStore(ctx.store.layout.root)
    candidate = candidates["description"][0]
    assert ctx.select_custom_text("description", "Keep this selection", None) is None
    before = other.layout.state_json.read_bytes()
    shown = cast(list[dict[str, str]], _assets_by_id(url)["description"]["candidates"])[0]
    assert shown["candidate_id"] == str(candidate.candidate_id)
    other.write_candidate(candidate.model_copy(update={"content": "NEW"}))
    # A second tab (or a refresh whose response never reached this page) reloads the new copy.
    reloaded = cast(list[dict[str, str]], _assets_by_id(url)["description"]["candidates"])[0]
    assert reloaded["content"] == "NEW"
    assert reloaded["candidate_digest"] != shown["candidate_digest"]

    status, receipt = _post_json(
        f"{url}/api/select",
        {
            "asset_id": "description",
            "candidate_id": shown["candidate_id"],
            "candidate_digest": shown["candidate_digest"],
        },
    )

    assert status == 409, receipt
    assert "reload candidates" in str(receipt["error"])
    assert other.layout.state_json.read_bytes() == before
    assert other.read_selected_text("description", TextFormat.markdown).strip() == "Keep this selection"

    status, receipt = _post_json(
        f"{url}/api/select",
        {
            "asset_id": "description",
            "candidate_id": reloaded["candidate_id"],
            "candidate_digest": reloaded["candidate_digest"],
        },
    )
    assert status == 200, receipt
    assert other.read_selected_text("description", TextFormat.markdown).strip() == "NEW"


def test_select_rejects_non_string_candidate_digest(pick_server: _PickServerTuple) -> None:
    _, url, _, candidates = pick_server
    status, receipt = _post_json(
        f"{url}/api/select",
        {
            "asset_id": "description",
            "candidate_id": str(candidates["description"][0].candidate_id),
            "candidate_digest": 1,
        },
    )
    assert status == 400, receipt


def test_done_refuses_active_refinement(pick_server: _PickServerTuple) -> None:
    _, url, ctx, _ = pick_server
    ctx.refining.add("description")
    status, data = _post_json(f"{url}/api/done", {})
    assert status == 409
    assert "pending" in str(data).lower()
    ctx.refining.clear()
    assert urllib.request.urlopen(f"{url}/api/assets").status == 200


def test_done_stops_server_after_acknowledgment_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, candidates = _setup_workspace(tmp_path)
    ctx = _ServerContext(
        store=store,
        candidates_by_asset=candidates,
        workspace_state=EpisodeWorkspace(episode_id="test_ep", root_dir="."),
        session_token=SESSION_TOKEN,
    )
    handler = object.__new__(_PickWebHandler)
    handler.ctx = ctx
    monkeypatch.setattr(handler, "_read_object", dict)
    statuses = []

    def respond(status: int, data: dict[str, object]) -> None:
        statuses.append(status)
        if status == 200:
            raise TimeoutError("Synthetic response write timeout")

    monkeypatch.setattr(handler, "_respond_json", respond)
    with pick_web._PickHTTPServer(("127.0.0.1", 0), partial(_PickWebHandler, ctx)) as server:
        handler.server = server
        ctx.refining.add("description")
        handler._handle_done()
        assert not server.stop_requested.is_set()

        ctx.refining.clear()
        with pytest.raises(TimeoutError, match="response write timeout"):
            handler._handle_done()
        assert ctx.closing
        assert statuses == [409, 200]
        assert server.stop_requested.is_set()


@pytest.mark.parametrize("failure", ["browser", "timer"])
def test_startup_failure_closes_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    import socket

    from podcast_pipeline.entrypoints import pick_web

    _setup_workspace(tmp_path)
    servers = []
    original = getattr(pick_web, "_PickHTTPServer", ThreadingHTTPServer)

    def capture(*args: Any, **kwargs: Any) -> HTTPServer:
        server = original(*args, **kwargs)
        servers.append(server)
        return server

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(pick_web, "_PickHTTPServer", capture, raising=False)
    monkeypatch.setattr(pick_web, "ThreadingHTTPServer", capture)
    if failure == "browser":
        monkeypatch.setattr(webbrowser, "open", fail)
    else:
        monkeypatch.setattr(pick_web.threading.Timer, "start", fail)
    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        pick_web.run_pick_web(workspace=tmp_path, asset_id=None)
    with socket.socket() as retry:
        retry.bind(servers[0].server_address)


def test_refine_failure_does_not_echo_private_diagnostics(
    pick_server: _PickServerTuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, url, ctx, _ = pick_server
    canary = "synthetic-transcript TOKEN-secret /private/synthetic"

    def fail(**kwargs: Any) -> Candidate:
        raise RuntimeError(canary)

    monkeypatch.setattr(asset_refine_llm, "refine_asset_text", fail)
    status, data = _post_json(f"{url}/api/refine", {"asset_id": "description", "instruction": "x", "text": "y"})
    assert status == 502
    assert data["code"] == "refinement_failed"
    assert data["reference"]
    assert canary not in json.dumps(data) + capsys.readouterr().err
    assert ctx.refining == set()


def test_done_waits_for_actual_refinement_publication(
    pick_server: _PickServerTuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, url, ctx, _ = pick_server
    started, release = threading.Event(), threading.Event()
    candidate = Candidate(asset_id="description", content="Completed synthetic refinement")

    def refine(**kwargs: Any) -> Candidate:
        started.set()
        assert release.wait(5)
        return candidate

    monkeypatch.setattr(asset_refine_llm, "refine_asset_text", refine)
    statuses = []
    worker = threading.Thread(
        target=lambda: statuses.append(
            _post_json(f"{url}/api/refine", {"asset_id": "description", "instruction": "x", "text": "y"})[0]
        )
    )
    worker.start()
    try:
        assert started.wait(5)
        assert _post_json(f"{url}/api/done", {})[0] == 409
        assert not ctx.store.layout.candidate_json_path("description", candidate.candidate_id).exists()
    finally:
        release.set()
        worker.join(5)
    assert statuses == [200]
    assert ctx.store.read_candidate("description", candidate.candidate_id) == candidate
    assert not ctx.refining
    assert _post_json(f"{url}/api/done", {})[0] == 200


def test_closing_rejects_new_mutations(pick_server: _PickServerTuple) -> None:
    _, url, ctx, candidates = pick_server
    ctx.closing = True
    status, _ = _post_json(
        f"{url}/api/select",
        {"asset_id": "description", "candidate_id": str(candidates["description"][0].candidate_id)},
    )
    assert status == 409
    assert not ctx.store.layout.state_json.exists()


def test_server_timeout_drains_handler_before_cli_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from podcast_pipeline.entrypoints import pick_web

    store, _ = _setup_workspace(tmp_path)
    opened, started, closing, release, finished = (threading.Event() for _ in range(5))
    urls: list[str] = []
    servers: list[Any] = []
    original_init = pick_web._PickHTTPServer.__init__
    original_close = pick_web._PickHTTPServer.server_close

    def capture(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        servers.append(self)

    def close(self: Any) -> None:
        closing.set()
        original_close(self)

    def browser(launch_uri: str) -> None:
        urls.append(launch_target(launch_uri))
        opened.set()

    candidate = Candidate(asset_id="description", content="Settled after timeout")

    def refine(**kwargs: Any) -> Candidate:
        started.set()
        assert release.wait(5)
        return candidate

    monkeypatch.setattr(pick_web._PickHTTPServer, "__init__", capture)
    monkeypatch.setattr(pick_web._PickHTTPServer, "server_close", close)
    monkeypatch.setattr(webbrowser, "open", browser)
    monkeypatch.setattr(asset_refine_llm, "refine_asset_text", refine)

    def run() -> None:
        try:
            pick_web.run_pick_web(workspace=tmp_path, asset_id=None)
        finally:
            finished.set()

    runner = threading.Thread(target=run)
    runner.start()
    client = None
    statuses: list[int] = []
    try:
        assert opened.wait(5)
        client = threading.Thread(
            target=lambda: statuses.append(
                _post_json(
                    f"{urls[0].partition('?')[0]}api/refine",
                    {"asset_id": "description", "instruction": "x", "text": "y"},
                    session_token=urls[0].partition("?token=")[2],
                )[0]
            )
        )
        client.start()
        assert started.wait(5)
        servers[0].stop_requested.set()  # Same path as the fallback timer.
        assert closing.wait(5)
        assert not finished.wait(0.1)
    finally:
        release.set()
        if client is not None:
            client.join(5)
        runner.join(5)
    assert finished.is_set()
    assert statuses == [200]
    assert store.read_candidate("description", candidate.candidate_id) == candidate


def test_body_deadline_is_absolute_during_trickle(
    pick_server: _PickServerTuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    from podcast_pipeline.entrypoints import pick_web

    server, _, ctx, _ = pick_server
    monkeypatch.setattr(pick_web, "_REQUEST_TIMEOUT_SECONDS", 0.15)
    stop = threading.Event()
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=1) as sock:
        sock.sendall(
            f"POST /api/select HTTP/1.0\r\nHost: 127.0.0.1:{server.server_port}\r\n"
            f"{SESSION_HEADER}: {SESSION_TOKEN}\r\n"
            "Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n".encode()
        )

        def trickle() -> None:
            while not stop.wait(0.02):
                try:
                    sock.sendall(b" ")
                except OSError:
                    break

        sender = threading.Thread(target=trickle)
        sender.start()
        try:
            assert b"400" in sock.recv(4096)
        finally:
            stop.set()
            sender.join(2)
    assert not ctx.store.layout.state_json.exists()


def test_page_carries_the_drafter_timeout_as_refine_deadline(
    pick_server: _PickServerTuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _server, base_url, _ctx, _candidates = pick_server
    monkeypatch.setenv("PODCAST_PIPELINE_AGENT_TIMEOUT", "300")
    body = urllib.request.urlopen(f"{base_url}/").read().decode("utf-8")
    assert "const REFINE_DEADLINE_MS = 360000;" in body
    assert "__REFINE_DEADLINE_MS__" not in body


def test_unreadable_state_answers_assets_without_diagnostics(
    pick_server: _PickServerTuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _server, base_url, ctx, _candidates = pick_server

    def broken() -> object:
        raise ValueError(f"synthetic failure in {ctx.store.layout.root}")

    monkeypatch.setattr(ctx, "get_assets_json", broken)
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(f"{base_url}/api/assets", timeout=5)
    assert error.value.code == 500
    payload = error.value.read().decode("utf-8")
    assert str(ctx.store.layout.root) not in payload
    assert "Local reference" in payload
    assert str(ctx.store.layout.root) not in capsys.readouterr().err


def test_huge_drafter_timeout_is_clamped_to_a_valid_browser_delay(
    pick_server: _PickServerTuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _server, base_url, _ctx, _candidates = pick_server
    monkeypatch.setenv("PODCAST_PIPELINE_AGENT_TIMEOUT", "1e9")
    body = urllib.request.urlopen(f"{base_url}/").read().decode("utf-8")
    assert "const REFINE_DEADLINE_MS = 2147483647;" in body
