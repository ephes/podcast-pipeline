"""Request guards and single-flight behaviour of the local dashboard / pick UI.

The Auphonic API is never called: ``run_produce`` tests patch ``AuphonicClient``.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Generator
from functools import partial
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest
import typer
from starlette.testclient import TestClient

from podcast_pipeline.auphonic_api import AuphonicApiError
from podcast_pipeline.dashboard_context import DashboardContext
from podcast_pipeline.domain.models import Asset, AssetKind, Candidate, EpisodeWorkspace
from podcast_pipeline.entrypoints import dashboard_web, produce
from podcast_pipeline.entrypoints.pick_web import _PickWebHandler, _ServerContext
from podcast_pipeline.local_web_guard import check_request
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore

_PORT = 8765
_BASE = f"http://127.0.0.1:{_PORT}"
_JSON = {"Content-Type": "application/json"}


def _workspace(tmp_path: Path) -> Path:
    EpisodeWorkspaceStore(tmp_path).write_episode_yaml({"episode_id": "ep1"})
    return tmp_path


@pytest.fixture()
def ctx(tmp_path: Path) -> DashboardContext:
    return DashboardContext(workspace=_workspace(tmp_path))


@pytest.fixture()
def client(ctx: DashboardContext) -> TestClient:
    return TestClient(dashboard_web.create_dashboard_app(ctx=ctx, port=_PORT), base_url=_BASE)


@pytest.fixture()
def no_job_threads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Record background job starts instead of running them (jobs stay "running")."""
    started: list[tuple[Any, ...]] = []
    monkeypatch.setattr(dashboard_web, "_start_daemon_thread", lambda target, *args: started.append((target, *args)))
    return started


# --- check_request -----------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected_port", "ok"),
    [
        ("127.0.0.1:8765", 8765, True),
        ("localhost:8765", 8765, True),
        ("[::1]:8765", 8765, True),
        ("127.0.0.1:8765", None, True),
        ("127.0.0.1:9999", 8765, False),
        ("127.0.0.1", 8765, False),
        ("evil.example:8765", 8765, False),
        ("127.0.0.1.evil.example:8765", 8765, False),
        ("127.0.0.1:87x65", 8765, False),
        ("", 8765, False),
        (None, 8765, False),
    ],
)
def test_check_request_host(host: str | None, expected_port: int | None, ok: bool) -> None:
    rejection = check_request(
        method="GET",
        host=host,
        origin=None,
        sec_fetch_site=None,
        content_type=None,
        expected_port=expected_port,
    )
    assert (rejection is None) is ok
    if rejection is not None:
        assert rejection.status == 403


def test_check_request_get_ignores_origin_and_content_type() -> None:
    assert (
        check_request(
            method="GET",
            host="127.0.0.1:1",
            origin="http://evil.example",
            sec_fetch_site="cross-site",
            content_type="text/plain",
            expected_port=1,
        )
        is None
    )


# --- dashboard middleware ----------------------------------------------------


def test_foreign_origin_post_is_rejected(client: TestClient, ctx: DashboardContext, no_job_threads: list[Any]) -> None:
    resp = client.post("/api/produce", json={}, headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403
    assert not ctx.jobs
    assert not no_job_threads


def test_null_origin_post_is_rejected(client: TestClient, ctx: DashboardContext) -> None:
    resp = client.post("/api/produce", json={}, headers={"Origin": "null"})
    assert resp.status_code == 403
    assert not ctx.jobs


def test_other_local_port_origin_is_rejected(client: TestClient, ctx: DashboardContext) -> None:
    resp = client.post("/api/produce", json={}, headers={"Origin": "http://127.0.0.1:3000"})
    assert resp.status_code == 403
    assert not ctx.jobs


def test_cross_site_fetch_metadata_is_rejected(client: TestClient, ctx: DashboardContext) -> None:
    resp = client.post("/api/produce", json={}, headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    assert not ctx.jobs


def test_foreign_host_is_rejected_for_reads_and_writes(ctx: DashboardContext) -> None:
    rebinding = TestClient(
        dashboard_web.create_dashboard_app(ctx=ctx, port=_PORT),
        base_url=f"http://attacker.example:{_PORT}",
    )
    assert rebinding.get("/api/episode").status_code == 403
    assert rebinding.post("/api/produce", json={}).status_code == 403
    assert not ctx.jobs


def test_wrong_port_host_is_rejected(ctx: DashboardContext) -> None:
    other = TestClient(dashboard_web.create_dashboard_app(ctx=ctx, port=_PORT), base_url="http://127.0.0.1:1")
    assert other.get("/api/episode").status_code == 403


def test_text_plain_post_is_rejected(client: TestClient, ctx: DashboardContext) -> None:
    resp = client.post("/api/produce", content=b"{}", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 415
    assert not ctx.jobs


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_put_and_delete_require_json(client: TestClient, method: str) -> None:
    resp = client.request(method, "/api/assets/description/notes", content=b'{"notes": "x"}')
    assert resp.status_code == 415


def test_same_origin_post_is_accepted(client: TestClient, ctx: DashboardContext, no_job_threads: list[Any]) -> None:
    resp = client.post(
        "/api/produce",
        json={},
        headers={"Origin": _BASE, "Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 200
    assert len(ctx.jobs) == 1
    assert len(no_job_threads) == 1


def test_get_dashboard_html_is_allowed(client: TestClient) -> None:
    assert client.get("/").status_code == 200


# --- single flight -----------------------------------------------------------


def test_second_produce_while_running_returns_409(
    client: TestClient, ctx: DashboardContext, no_job_threads: list[Any]
) -> None:
    first = client.post("/api/produce", json={})
    assert first.status_code == 200
    second = client.post("/api/produce", json={})
    assert second.status_code == 409
    assert second.json()["job_id"] == first.json()["job_id"]
    assert len(ctx.jobs) == 1
    assert len(no_job_threads) == 1


def test_produce_can_restart_after_job_finishes(
    client: TestClient, ctx: DashboardContext, no_job_threads: list[Any]
) -> None:
    first = client.post("/api/produce", json={})
    with ctx.lock:
        ctx.jobs[first.json()["job_id"]].status = "failed"
    assert client.post("/api/produce", json={}).status_code == 200
    assert len(no_job_threads) == 2


def test_second_transcribe_while_running_returns_409(client: TestClient, no_job_threads: list[Any]) -> None:
    assert client.post("/api/transcribe", json={}).status_code == 200
    assert client.post("/api/transcribe", json={}).status_code == 409


def test_regenerate_single_flight_is_per_asset(client: TestClient, no_job_threads: list[Any]) -> None:
    assert client.post("/api/assets/description/regenerate", json={}).status_code == 200
    assert client.post("/api/assets/description/regenerate", json={}).status_code == 409
    assert client.post("/api/assets/shownotes/regenerate", json={}).status_code == 200


def test_concurrent_produce_posts_start_one_job(ctx: DashboardContext, no_job_threads: list[Any]) -> None:
    app = dashboard_web.create_dashboard_app(ctx=ctx, port=_PORT)
    statuses: list[int] = []
    barrier = threading.Barrier(8)

    def post() -> None:
        with TestClient(app, base_url=_BASE) as c:
            barrier.wait()
            statuses.append(c.post("/api/produce", json={}).status_code)

    threads = [threading.Thread(target=post) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [200] + [409] * 7
    assert len(no_job_threads) == 1


# --- count clamping ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1000, dashboard_web.MAX_CANDIDATES), (5, 5), (0, 3), (-1, 3), ("9", 3), (True, 3), (None, 3)],
)
def test_regenerate_candidates_is_clamped(
    client: TestClient, no_job_threads: list[Any], value: object, expected: int
) -> None:
    assert client.post("/api/assets/description/regenerate", json={"candidates": value}).status_code == 200
    _target, _ctx, _job, asset_id, count = no_job_threads[0]
    assert asset_id == "description"
    assert count == expected


@pytest.mark.parametrize("path", ["/api/draft", "/api/draft/candidates"])
def test_draft_candidates_is_clamped(client: TestClient, no_job_threads: list[Any], path: str) -> None:
    assert client.post(path, json={"candidates": 1000}).status_code == 200
    assert no_job_threads[0][3] == dashboard_web.MAX_CANDIDATES


def test_review_iterations_is_clamped(client: TestClient, no_job_threads: list[Any]) -> None:
    resp = client.post("/api/review", json={"asset_id": "description", "max_iterations": 1000})
    assert resp.status_code == 200
    assert no_job_threads[0][4] == dashboard_web.MAX_REVIEW_ITERATIONS


# --- run_produce: per-workspace lock and resume (Auphonic mocked) -------------


class _FakeProduction:
    def __init__(self, uuid: str) -> None:
        self.uuid = uuid
        self.output_files = [{"download_url": "https://example.invalid/out.mp3"}]


class _FakeAuphonicClient:
    started: list[str] = []
    waited: list[str] = []
    on_start: Any = None

    def __init__(self, _credentials: object) -> None:
        pass

    def __enter__(self) -> _FakeAuphonicClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def start_production(self, _payload: object, *, on_created: Any = None) -> _FakeProduction:
        if _FakeAuphonicClient.on_start is not None:
            _FakeAuphonicClient.on_start()
        uuid = f"prod-{len(_FakeAuphonicClient.started) + 1}"
        if on_created is not None:
            on_created(uuid)
        _FakeAuphonicClient.started.append(uuid)
        return _FakeProduction(uuid)

    def wait_for_production(self, uuid: str, **_kwargs: object) -> _FakeProduction:
        _FakeAuphonicClient.waited.append(uuid)
        return _FakeProduction(uuid)

    def list_output_files(self, _uuid: str) -> list[object]:
        return []

    def download_outputs(self, _files: object, _target: Path) -> None:
        return None


@pytest.fixture()
def fake_auphonic(monkeypatch: pytest.MonkeyPatch) -> Generator[type[_FakeAuphonicClient], None, None]:
    _FakeAuphonicClient.started = []
    _FakeAuphonicClient.waited = []
    _FakeAuphonicClient.on_start = None
    monkeypatch.setattr(produce, "AuphonicClient", _FakeAuphonicClient)
    monkeypatch.setattr(produce, "load_auphonic_credentials", lambda: object())
    monkeypatch.setattr(produce, "build_auphonic_payload", lambda **_kwargs: {})
    yield _FakeAuphonicClient


def test_run_produce_starts_once_then_resumes(tmp_path: Path, fake_auphonic: type[_FakeAuphonicClient]) -> None:
    workspace = _workspace(tmp_path)
    produce.run_produce(workspace=workspace, dry_run=False)
    produce.run_produce(workspace=workspace, dry_run=False)
    assert fake_auphonic.started == ["prod-1"]
    assert fake_auphonic.waited == ["prod-1", "prod-1"]


def test_run_produce_rejects_concurrent_run_for_same_workspace(
    tmp_path: Path, fake_auphonic: type[_FakeAuphonicClient]
) -> None:
    workspace = _workspace(tmp_path)
    nested_errors: list[str] = []

    def start_concurrent_run() -> None:
        try:
            produce.run_produce(workspace=workspace, dry_run=False)
        except typer.BadParameter as exc:
            nested_errors.append(str(exc))

    fake_auphonic.on_start = start_concurrent_run
    produce.run_produce(workspace=workspace, dry_run=False)

    assert fake_auphonic.started == ["prod-1"]
    assert len(nested_errors) == 1
    assert "already running" in nested_errors[0]
    state = EpisodeWorkspaceStore(workspace).read_state()
    assert isinstance(state, EpisodeWorkspace)
    assert state.auphonic_production_uuid == "prod-1"


def test_stale_dashboard_state_does_not_drop_production_uuid(
    tmp_path: Path, fake_auphonic: type[_FakeAuphonicClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET status caches state; produce saves a UUID then fails; a later selection must keep the UUID."""
    store = EpisodeWorkspaceStore(_workspace(tmp_path))
    candidate = Candidate(asset_id="description", content="# Description\n\nText.")
    store.write_candidate(candidate)
    ctx = DashboardContext(workspace=tmp_path)
    client = TestClient(dashboard_web.create_dashboard_app(ctx=ctx, port=_PORT), base_url=_BASE)
    assert client.get("/api/status").status_code == 200  # caches workspace state without a UUID

    def wait_times_out(self: _FakeAuphonicClient, uuid: str, **_kwargs: object) -> _FakeProduction:
        raise AuphonicApiError("timed out")

    monkeypatch.setattr(_FakeAuphonicClient, "wait_for_production", wait_times_out)
    with pytest.raises(typer.BadParameter):
        produce.run_produce(workspace=tmp_path, dry_run=False)
    assert store.read_state().auphonic_production_uuid == "prod-1"

    resp = client.post(
        "/api/select",
        json={"asset_id": "description", "candidate_id": str(candidate.candidate_id)},
    )
    assert resp.status_code == 200
    state = store.read_state()
    assert state.auphonic_production_uuid == "prod-1"
    assert any(asset.selected_candidate_id == candidate.candidate_id for asset in state.assets)

    monkeypatch.undo()
    monkeypatch.setattr(produce, "AuphonicClient", _FakeAuphonicClient)
    monkeypatch.setattr(produce, "load_auphonic_credentials", lambda: object())
    monkeypatch.setattr(produce, "build_auphonic_payload", lambda **_kwargs: {})
    produce.run_produce(workspace=tmp_path, dry_run=False)
    assert fake_auphonic.started == ["prod-1"]


def test_set_production_uuid_keeps_concurrent_selection(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(_workspace(tmp_path))
    stale = EpisodeWorkspace(episode_id="ep1", root_dir=".")
    candidate = Candidate(asset_id="description", content="x")
    selected = stale.model_copy(
        update={
            "assets": [
                Asset(
                    asset_id="description",
                    kind=AssetKind.description,
                    candidates=[candidate],
                    selected_candidate_id=candidate.candidate_id,
                )
            ]
        }
    )
    store.write_state(selected)
    store.set_auphonic_production_uuid("prod-9", default=stale)
    state = store.read_state()
    assert state.auphonic_production_uuid == "prod-9"
    assert state.assets[0].selected_candidate_id == candidate.candidate_id


# --- pick UI -----------------------------------------------------------------


@pytest.fixture()
def pick_port(tmp_path: Path) -> Generator[int, None, None]:
    store = EpisodeWorkspaceStore(_workspace(tmp_path))
    ctx = _ServerContext(
        store=store,
        candidates_by_asset={},
        workspace_state=EpisodeWorkspace(episode_id="ep1", root_dir="."),
    )
    server = HTTPServer(("127.0.0.1", 0), partial(_PickWebHandler, ctx))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield int(server.server_address[1])
    server.shutdown()
    server.server_close()


def _pick_request(port: int, method: str, path: str, headers: dict[str, str], body: bytes | None = None) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True)
        for name, value in headers.items():
            conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        return conn.getresponse().status
    finally:
        conn.close()


def test_pick_web_rejects_foreign_host(pick_port: int) -> None:
    assert _pick_request(pick_port, "GET", "/api/assets", {"Host": f"evil.example:{pick_port}"}) == 403


def test_pick_web_rejects_foreign_origin(pick_port: int) -> None:
    headers = {"Host": f"127.0.0.1:{pick_port}", "Origin": "http://evil.example", **_JSON}
    assert _pick_request(pick_port, "POST", "/api/done", headers, b"{}") == 403


def test_pick_web_rejects_text_plain(pick_port: int) -> None:
    headers = {"Host": f"127.0.0.1:{pick_port}", "Content-Type": "text/plain"}
    body = json.dumps({"asset_id": "description", "candidate_id": "x"}).encode()
    assert _pick_request(pick_port, "POST", "/api/select", headers, body) == 415


def test_pick_web_accepts_same_origin_json(pick_port: int) -> None:
    headers = {"Host": f"127.0.0.1:{pick_port}", "Origin": f"http://127.0.0.1:{pick_port}", **_JSON}
    body = json.dumps({"asset_id": "description", "candidate_id": "x"}).encode()
    # Reaches the handler: unknown candidate -> 400, not a guard rejection.
    assert _pick_request(pick_port, "POST", "/api/select", headers, body) == 400


def test_stale_dashboard_state_does_not_roll_back_restarted_production_uuid(
    tmp_path: Path, fake_auphonic: type[_FakeAuphonicClient]
) -> None:
    """A dashboard opened before ``produce --restart`` must not restore the old UUID on its next write."""
    store = EpisodeWorkspaceStore(_workspace(tmp_path))
    store.write_state(EpisodeWorkspace(episode_id="ep_001", root_dir=".", auphonic_production_uuid="old-prod"))
    candidate = Candidate(asset_id="description", content="# Description\n\nText.")
    store.write_candidate(candidate)
    ctx = DashboardContext(workspace=tmp_path)
    client = TestClient(dashboard_web.create_dashboard_app(ctx=ctx, port=_PORT), base_url=_BASE)
    assert client.get("/api/status").status_code == 200  # caches state with old-prod

    produce.run_produce(workspace=tmp_path, dry_run=False, restart=True)
    assert store.read_state().auphonic_production_uuid == "prod-1"

    resp = client.post(
        "/api/select",
        json={"asset_id": "description", "candidate_id": str(candidate.candidate_id)},
    )
    assert resp.status_code == 200
    state = store.read_state()
    assert state.auphonic_production_uuid == "prod-1"
    assert any(asset.selected_candidate_id == candidate.candidate_id for asset in state.assets)


# --- agent CLI timeout ---------------------------------------------------------


def test_timed_out_summarize_job_fails_and_frees_the_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung drafter CLI must end the summarize job as failed so a retry is accepted (200, not 409)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = EpisodeWorkspaceStore(workspace)
    store.write_episode_yaml({"episode_id": "ep1"})
    chunk_path = store.layout.transcript_chunk_text_path(1)
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    chunk_path.write_text("Hello world transcript chunk.", encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents:\n  drafter:\n    command: sleep\n    args: ['30']\n", encoding="utf-8")
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(config_path))
    monkeypatch.setenv("PODCAST_PIPELINE_AGENT_TIMEOUT", "0.3")

    ctx = DashboardContext(workspace=workspace)
    client = TestClient(dashboard_web.create_dashboard_app(ctx=ctx, port=_PORT), base_url=_BASE)

    first = client.post("/api/draft/summarize", json={})
    assert first.status_code == 200
    job_id = first.json()["job_id"]

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with ctx.lock:
            job = ctx.jobs[job_id]
            if job.status != "running":
                break
        time.sleep(0.05)
    with ctx.lock:
        job = ctx.jobs[job_id]
        assert job.status == "failed"
        assert job.error == "Drafter CLI timed out after 0.3 s"

    monkeypatch.setattr(dashboard_web, "_start_daemon_thread", lambda target, *args: None)
    second = client.post("/api/draft/summarize", json={})
    assert second.status_code == 200
    assert second.json()["job_id"] != job_id
