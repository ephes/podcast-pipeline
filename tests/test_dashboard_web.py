from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest
import uvicorn

from podcast_pipeline.dashboard_context import DashboardContext
from podcast_pipeline.domain.models import Candidate
from podcast_pipeline.entrypoints.dashboard_web import create_dashboard_app
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def _setup_workspace(tmp_path: Path) -> EpisodeWorkspaceStore:
    """Create a workspace with candidates for testing."""
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "test_ep", "hosts": ["Alice", "Bob"]})

    c1 = Candidate(asset_id="description", content="# Description 1\n\nFirst.")
    c2 = Candidate(asset_id="description", content="# Description 2\n\nSecond.")
    c3 = Candidate(asset_id="shownotes", content="# Shownotes\n\nNotes.")

    store.write_candidate(c1)
    store.write_candidate(c2)
    store.write_candidate(c3)

    return store


_DashboardServerTuple = tuple[uvicorn.Server, str, DashboardContext]


def _start_dashboard_server(ctx: DashboardContext) -> tuple[uvicorn.Server, socket.socket, threading.Thread, str]:
    holder: dict[str, uvicorn.Server] = {}

    def request_shutdown() -> None:
        server = holder.get("server")
        if server is not None:
            server.should_exit = True

    app = create_dashboard_app(ctx=ctx, on_done=request_shutdown)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    host = str(sock.getsockname()[0])
    port = int(sock.getsockname()[1])
    base_url = f"http://{host}:{port}"

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        access_log=False,
        log_level="error",
    )
    server = uvicorn.Server(config=config)
    holder["server"] = server

    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    thread.start()

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/api/jobs", timeout=0.25)
            break
        except Exception:
            time.sleep(0.05)

    return server, sock, thread, base_url


@pytest.fixture()
def dashboard_server(
    tmp_path: Path,
) -> Generator[_DashboardServerTuple, None, None]:
    _setup_workspace(tmp_path)

    ctx = DashboardContext(workspace=tmp_path)
    server, sock, thread, base_url = _start_dashboard_server(ctx)

    yield server, base_url, ctx

    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


@pytest.fixture()
def bare_dashboard_server(
    tmp_path: Path,
) -> Generator[_DashboardServerTuple, None, None]:
    """Dashboard server on a workspace with no episode.yaml."""
    ctx = DashboardContext(workspace=tmp_path)
    server, sock, thread, base_url = _start_dashboard_server(ctx)

    yield server, base_url, ctx

    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


def test_get_root_returns_html(dashboard_server: _DashboardServerTuple) -> None:
    _server, base_url, _ctx = dashboard_server
    resp = urllib.request.urlopen(f"{base_url}/")
    assert resp.status == 200
    body = resp.read().decode("utf-8")
    assert "<html" in body
    assert "Podcast Pipeline" in body
    assert "onclick=" not in body


def test_get_api_status(dashboard_server: _DashboardServerTuple) -> None:
    _server, base_url, _ctx = dashboard_server
    resp = urllib.request.urlopen(f"{base_url}/api/status")
    assert resp.status == 200
    data = json.loads(resp.read().decode("utf-8"))
    assert data["episode_id"] == "test_ep"
    assert "stages" in data
    assert data["stages"]["episode_yaml"] is True


def test_get_api_episode(dashboard_server: _DashboardServerTuple) -> None:
    _server, base_url, _ctx = dashboard_server
    resp = urllib.request.urlopen(f"{base_url}/api/episode")
    assert resp.status == 200
    data = json.loads(resp.read().decode("utf-8"))
    assert data["episode_id"] == "test_ep"
    assert data["hosts"] == ["Alice", "Bob"]
    assert data["editorial_notes"] == {}


def test_post_api_episode_updates_metadata(
    dashboard_server: _DashboardServerTuple,
) -> None:
    _server, base_url, _ctx = dashboard_server
    payload = json.dumps({"hosts": ["Charlie"]}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/episode",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200

    resp2 = urllib.request.urlopen(f"{base_url}/api/episode")
    data = json.loads(resp2.read().decode("utf-8"))
    assert data["hosts"] == ["Charlie"]


def test_post_api_episode_ignores_empty_episode_id(
    dashboard_server: _DashboardServerTuple,
) -> None:
    """Empty episode_id is ignored instead of crashing the handler."""
    _server, base_url, _ctx = dashboard_server
    payload = json.dumps({"episode_id": ""}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/episode",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200

    resp2 = urllib.request.urlopen(f"{base_url}/api/episode")
    data = json.loads(resp2.read().decode("utf-8"))
    assert data["episode_id"] == "test_ep"


def test_post_api_episode_without_episode_id_on_bare_workspace(
    bare_dashboard_server: _DashboardServerTuple,
) -> None:
    """Updating hosts on a workspace without episode.yaml returns 400 (episode_id required)."""
    _server, base_url, _ctx = bare_dashboard_server
    payload = json.dumps({"hosts": ["Alice"]}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/episode",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        pytest.fail("Expected HTTP 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
        body = json.loads(exc.read().decode("utf-8"))
        assert "episode_id" in body["error"]


def test_post_api_episode_filters_invalid_host_types(
    dashboard_server: _DashboardServerTuple,
) -> None:
    """Non-string hosts are silently dropped instead of crashing the handler."""
    _server, base_url, _ctx = dashboard_server
    payload = json.dumps({"hosts": ["Alice", 42, None, "Bob"]}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/episode",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200

    resp2 = urllib.request.urlopen(f"{base_url}/api/episode")
    data = json.loads(resp2.read().decode("utf-8"))
    assert data["hosts"] == ["Alice", "Bob"]


def test_get_api_assets(dashboard_server: _DashboardServerTuple) -> None:
    _server, base_url, _ctx = dashboard_server
    resp = urllib.request.urlopen(f"{base_url}/api/assets")
    assert resp.status == 200
    data = json.loads(resp.read().decode("utf-8"))
    assert isinstance(data, list)
    assert len(data) == 2
    asset_ids = {a["asset_id"] for a in data}
    assert asset_ids == {"description", "shownotes"}


def test_post_api_select(dashboard_server: _DashboardServerTuple) -> None:
    _server, base_url, ctx = dashboard_server
    # Get candidates first
    resp = urllib.request.urlopen(f"{base_url}/api/assets")
    assets = json.loads(resp.read().decode("utf-8"))
    desc = next(a for a in assets if a["asset_id"] == "description")
    cid = desc["candidates"][0]["candidate_id"]

    payload = json.dumps({"asset_id": "description", "candidate_id": cid}).encode()
    req = urllib.request.Request(
        f"{base_url}/api/select",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200
    assert json.loads(resp.read().decode("utf-8"))["ok"] is True

    # Verify selection persisted
    resp2 = urllib.request.urlopen(f"{base_url}/api/assets")
    assets2 = json.loads(resp2.read().decode("utf-8"))
    desc2 = next(a for a in assets2 if a["asset_id"] == "description")
    assert desc2["selected_candidate_id"] == cid


def test_editorial_notes_crud(dashboard_server: _DashboardServerTuple) -> None:
    _server, base_url, _ctx = dashboard_server

    # Get (initially empty)
    resp = urllib.request.urlopen(f"{base_url}/api/assets/description/notes")
    data = json.loads(resp.read().decode("utf-8"))
    assert data["notes"] == ""

    # Put
    payload = json.dumps({"notes": "More detail please"}).encode()
    req = urllib.request.Request(
        f"{base_url}/api/assets/description/notes",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200

    # Get after put
    resp = urllib.request.urlopen(f"{base_url}/api/assets/description/notes")
    data = json.loads(resp.read().decode("utf-8"))
    assert data["notes"] == "More detail please"

    # Delete
    req = urllib.request.Request(
        f"{base_url}/api/assets/description/notes",
        method="DELETE",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200

    # Get after delete
    resp = urllib.request.urlopen(f"{base_url}/api/assets/description/notes")
    data = json.loads(resp.read().decode("utf-8"))
    assert data["notes"] == ""


def test_get_api_jobs_empty(dashboard_server: _DashboardServerTuple) -> None:
    _server, base_url, _ctx = dashboard_server
    resp = urllib.request.urlopen(f"{base_url}/api/jobs")
    data = json.loads(resp.read().decode("utf-8"))
    assert data == []


def test_job_endpoints_serialize_bytes_progress(dashboard_server: _DashboardServerTuple) -> None:
    """Bytes in job progress are normalized instead of crashing JSON/SSE responses."""
    _server, base_url, ctx = dashboard_server

    with ctx.lock:
        job = ctx.create_job("draft")
        job.progress.append(cast(Any, b"binary-progress-line"))
        job.status = "completed"

    resp = urllib.request.urlopen(f"{base_url}/api/jobs/{job.job_id}")
    assert resp.status == 200
    payload = json.loads(resp.read().decode("utf-8"))
    assert payload["progress"] == ["binary-progress-line"]

    stream_resp = urllib.request.urlopen(
        f"{base_url}/api/jobs/{job.job_id}/stream",
        timeout=2,
    )
    assert stream_resp.status == 200
    stream_lines: list[str] = []
    for _ in range(12):
        line = stream_resp.readline().decode("utf-8")
        if not line:
            break
        stream_lines.append(line)
    stream_resp.close()

    # SSE must be newline-delimited: each event is "data: ...\\n\\n".
    data_lines = [line for line in stream_lines if line.startswith("data: ")]
    assert len(data_lines) >= 2
    assert "\n" in stream_lines

    stream_text = "".join(stream_lines)
    assert '"type": "progress"' in stream_text
    assert '"message": "binary-progress-line"' in stream_text
    assert '"type": "done"' in stream_text


def test_get_unknown_job_stream_returns_404(dashboard_server: _DashboardServerTuple) -> None:
    _server, base_url, _ctx = dashboard_server
    try:
        urllib.request.urlopen(f"{base_url}/api/jobs/does-not-exist/stream")
        pytest.fail("Expected HTTP 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_post_api_draft_creates_job(
    dashboard_server: _DashboardServerTuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/draft returns a job_id and registers a job in context."""
    from podcast_pipeline.entrypoints import dashboard_web

    _server, base_url, ctx = dashboard_server

    # Prevent heavy background work; keep threading behavior intact.
    monkeypatch.setattr(dashboard_web, "_run_draft_job", lambda *_args, **_kwargs: None)

    payload = json.dumps({"candidates": 3}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/draft",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200
    data = json.loads(resp.read().decode("utf-8"))
    assert "job_id" in data

    with ctx.lock:
        job = ctx.jobs.get(data["job_id"])
        assert job is not None
        assert job.stage == "draft"
        assert job.status == "running"


def test_get_unknown_route_returns_404(
    dashboard_server: _DashboardServerTuple,
) -> None:
    _server, base_url, _ctx = dashboard_server
    try:
        urllib.request.urlopen(f"{base_url}/nonexistent")
        pytest.fail("Expected HTTP 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_post_unknown_route_returns_404(
    dashboard_server: _DashboardServerTuple,
) -> None:
    _server, base_url, _ctx = dashboard_server
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


def test_put_unknown_route_returns_404(
    dashboard_server: _DashboardServerTuple,
) -> None:
    _server, base_url, _ctx = dashboard_server
    req = urllib.request.Request(
        f"{base_url}/nonexistent",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        urllib.request.urlopen(req)
        pytest.fail("Expected HTTP 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_delete_unknown_route_returns_404(
    dashboard_server: _DashboardServerTuple,
) -> None:
    _server, base_url, _ctx = dashboard_server
    req = urllib.request.Request(
        f"{base_url}/nonexistent",
        method="DELETE",
    )
    try:
        urllib.request.urlopen(req)
        pytest.fail("Expected HTTP 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_draft_summarize_invalid_json_returns_400_no_job(
    dashboard_server: _DashboardServerTuple,
) -> None:
    """Invalid JSON on /api/draft/summarize must return 400 without creating a job."""
    _server, base_url, ctx = dashboard_server
    req = urllib.request.Request(
        f"{base_url}/api/draft/summarize",
        data=b"not-json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        pytest.fail("Expected HTTP 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    # No job should have been created
    assert len(ctx.jobs) == 0


def test_produce_invalid_json_returns_400_no_job(
    dashboard_server: _DashboardServerTuple,
) -> None:
    """Invalid JSON on /api/produce must return 400 without creating a job."""
    _server, base_url, ctx = dashboard_server
    req = urllib.request.Request(
        f"{base_url}/api/produce",
        data=b"[1,2,3]",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        pytest.fail("Expected HTTP 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    assert len(ctx.jobs) == 0


def test_produce_preview_without_config(
    dashboard_server: _DashboardServerTuple,
) -> None:
    """produce/preview returns 400 when no auphonic config exists."""
    _server, base_url, _ctx = dashboard_server
    req = urllib.request.Request(
        f"{base_url}/api/produce/preview",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        pytest.fail("Expected HTTP 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_dashboard_context_status(tmp_path: Path) -> None:
    _setup_workspace(tmp_path)
    ctx = DashboardContext(workspace=tmp_path)
    status = ctx.get_status_json()
    assert status["episode_id"] == "test_ep"
    assert status["hosts"] == ["Alice", "Bob"]
    assert status["stages"]["episode_yaml"] is True
    assert status["stages"]["candidates"] == 3


def test_dashboard_context_editorial_notes(tmp_path: Path) -> None:
    _setup_workspace(tmp_path)
    ctx = DashboardContext(workspace=tmp_path)

    assert ctx.get_editorial_notes("description") == ""
    ctx.set_editorial_notes("description", "test note")
    assert ctx.get_editorial_notes("description") == "test note"
    ctx.clear_editorial_notes("description")
    assert ctx.get_editorial_notes("description") == ""


def test_dashboard_context_episode_update(tmp_path: Path) -> None:
    _setup_workspace(tmp_path)
    ctx = DashboardContext(workspace=tmp_path)

    ctx.update_episode({"hosts": ["NewHost"]})
    episode = ctx.get_episode_json()
    assert episode["hosts"] == ["NewHost"]


def test_run_dashboard_nonexistent_workspace(tmp_path: Path) -> None:
    """run_dashboard exits with SystemExit(1) for a nonexistent workspace."""
    from podcast_pipeline.entrypoints.dashboard_web import run_dashboard

    with pytest.raises(SystemExit, match="1"):
        run_dashboard(workspace=tmp_path / "does_not_exist")


def test_run_dashboard_opens_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify run_dashboard opens a browser."""
    _setup_workspace(tmp_path)

    opened_urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened_urls.append(url))

    from podcast_pipeline.entrypoints import dashboard_web

    monkeypatch.setattr(dashboard_web, "_run_uvicorn_server", lambda _server, _sock: None)

    dashboard_web.run_dashboard(workspace=tmp_path)

    assert len(opened_urls) == 1
    assert opened_urls[0].startswith("http://127.0.0.1:")
