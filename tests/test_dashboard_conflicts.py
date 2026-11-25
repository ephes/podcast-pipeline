import threading
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from starlette.testclient import TestClient

from podcast_pipeline import dashboard_context
from podcast_pipeline.dashboard_context import BackgroundJob, DashboardContext
from podcast_pipeline.domain.models import Candidate
from podcast_pipeline.entrypoints import dashboard_web
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore
from tests.session_support import SESSION_HEADERS, SESSION_TOKEN


def poll_job(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 2
    while True:
        job: dict[str, Any] = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running" or time.monotonic() >= deadline:
            return job
        time.sleep(0.01)


@pytest.mark.parametrize(
    ("first", "body", "blocked"),
    [
        ("/api/draft", {}, "/api/transcribe"),
        ("/api/draft", {}, "/api/assets/description/refine"),
        ("/api/assets/description/refine", {"instruction": "shorten", "text": "synthetic"}, "/api/review"),
        ("/api/draft", {}, "/api/draft/summarize"),
        ("/api/transcribe", {}, "/api/draft/candidates"),
        ("/api/draft/summarize", {}, "/api/draft"),
        ("/api/draft/candidates", {}, "/api/assets/description/regenerate"),
        ("/api/review", {"asset_id": "description"}, "/api/assets/description/regenerate"),
        ("/api/assets/description/regenerate", {}, "/api/draft"),
    ],
)
def test_conflicting_jobs_are_rejected_before_worker_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str, body: dict[str, Any], blocked: str
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "synthetic"})
    store.write_candidate(Candidate(asset_id="description", content="Synthetic candidate"))
    ctx = DashboardContext(workspace=tmp_path)
    entered, release = threading.Event(), threading.Event()
    threads = []
    started = []
    failures = []

    def worker(context: DashboardContext, job: Any, *args: Any) -> None:
        try:
            entered.set()
            assert release.wait(5)
        except BaseException as exc:
            failures.append(exc)
        finally:
            with context.lock:
                context.finish_job(job, "failed")

    def start(target: Any, context: DashboardContext, job: Any, *args: Any) -> None:
        started.append(job.stage)
        thread = threading.Thread(target=worker, args=(context, job, *args))
        threads.append(thread)
        thread.start()

    monkeypatch.setattr(dashboard_web, "_start_daemon_thread", start)
    with TestClient(
        dashboard_web.create_dashboard_app(ctx=ctx, port=8765, session_token=SESSION_TOKEN),
        base_url="http://127.0.0.1:8765",
        headers=SESSION_HEADERS,
    ) as client:
        try:
            initial = client.post(first, json=body)
            assert initial.status_code == 200
            assert entered.wait(5)
            blocked_body = (
                {"asset_id": "description"}
                if blocked == "/api/review"
                else ({"instruction": "shorten", "text": "synthetic"} if blocked.endswith("/refine") else {})
            )
            response = client.post(blocked, json=blocked_body)
            assert response.status_code == 409
            assert response.json()["job_id"] == initial.json()["job_id"]
            assert started == [ctx.jobs[initial.json()["job_id"]].stage]
            # Paid production is independent of draft cache generation in this local policy.
            independent = client.post("/api/produce", json={})
            assert independent.status_code == 200 and len(started) == 2
        finally:
            release.set()
            for thread in threads:
                thread.join(5)
        assert all(not thread.is_alive() for thread in threads)
        assert not failures
        assert client.post(blocked, json=blocked_body).status_code == 200
    for thread in threads:
        thread.join(5)


def test_jobs_with_shared_uuid_prefix_remain_addressable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first_id = UUID("12345678-0000-4000-8000-000000000001")
    second_id = UUID("12345678-0000-4000-8000-000000000002")
    ids = iter((first_id, first_id, second_id))
    monkeypatch.setattr(dashboard_context.uuid, "uuid4", lambda: next(ids))
    ctx = DashboardContext(workspace=tmp_path)

    with ctx.lock:
        draft = ctx.create_job("draft")
        produce = ctx.create_job("produce")
        assert ctx.jobs[draft.job_id] is draft
        assert ctx.jobs[produce.job_id] is produce
        assert ctx.running_job("transcribe") is draft

        ctx.finish_job(draft, "completed")
        assert ctx.jobs[draft.job_id] is draft
        assert ctx.running_job("produce") is produce


def test_finish_job_cannot_replace_another_record(tmp_path: Path) -> None:
    ctx = DashboardContext(workspace=tmp_path)

    with ctx.lock:
        active = ctx.create_job("draft")
        stale = BackgroundJob(job_id=active.job_id, stage="produce")
        ctx.finish_job(stale, "completed")
        assert stale.status == "completed"
        assert ctx.jobs[active.job_id] is active
        assert ctx.running_job("draft") is active


@pytest.mark.filterwarnings("ignore:Exception in thread .*:pytest.PytestUnhandledThreadExceptionWarning")
def test_worker_exit_settles_job_and_permits_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from podcast_pipeline.entrypoints import produce

    ctx = DashboardContext(workspace=tmp_path)
    attempts = 0

    def run_produce(**kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SystemExit("synthetic worker exit")

    monkeypatch.setattr(produce, "run_produce", run_produce)
    with TestClient(
        dashboard_web.create_dashboard_app(ctx=ctx, session_token=SESSION_TOKEN),
        base_url="http://127.0.0.1",
        headers=SESSION_HEADERS,
    ) as client:
        response = client.post("/api/produce", json={})
        assert response.status_code == 200
        job_id = response.json()["job_id"]

        failed = poll_job(client, job_id)
        assert failed["status"] == "failed"
        assert failed["error"] == "Background worker exited unexpectedly"

        retry = client.post("/api/produce", json={})
        assert retry.status_code == 200
        completed = poll_job(client, retry.json()["job_id"])
        assert completed["status"] == "completed"
        assert completed["error"] is None


def test_done_waits_for_publication_and_then_closes_admission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from podcast_pipeline.entrypoints import produce

    ctx = DashboardContext(workspace=tmp_path)
    publishing, release, shutdown = threading.Event(), threading.Event(), threading.Event()

    def run_produce(**kwargs: Any) -> None:
        publishing.set()
        assert release.wait(5)

    monkeypatch.setattr(produce, "run_produce", run_produce)
    with TestClient(
        dashboard_web.create_dashboard_app(ctx=ctx, on_done=shutdown.set, session_token=SESSION_TOKEN),
        base_url="http://127.0.0.1",
        headers=SESSION_HEADERS,
    ) as client:
        first = client.post("/api/produce", json={})
        assert first.status_code == 200
        job_id = first.json()["job_id"]
        try:
            assert publishing.wait(5)
            blocked = client.post("/api/done", json={})
            assert blocked.status_code == 409
            assert blocked.json()["job_id"] == job_id
            assert not shutdown.is_set()
            assert client.get(f"/api/jobs/{job_id}").json()["status"] == "running"
        finally:
            release.set()
            completed = poll_job(client, job_id)

        assert completed["status"] == "completed"
        assert client.post("/api/done", json={}).status_code == 200
        assert shutdown.wait(5)
        assert client.post("/api/produce", json={}).status_code == 503
        assert client.get("/api/jobs").json() == [
            {"job_id": job_id, "stage": "produce", "status": "completed", "error": None}
        ]


def test_different_assets_remain_independent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    EpisodeWorkspaceStore(tmp_path).write_episode_yaml({"episode_id": "synthetic"})
    ctx = DashboardContext(workspace=tmp_path)
    starts = []
    monkeypatch.setattr(dashboard_web, "_start_daemon_thread", lambda *args: starts.append(args))
    with TestClient(
        dashboard_web.create_dashboard_app(ctx=ctx, port=8765, session_token=SESSION_TOKEN),
        base_url="http://127.0.0.1:8765",
        headers=SESSION_HEADERS,
    ) as client:
        assert client.post("/api/review", json={"asset_id": "description"}).status_code == 200
        assert client.post("/api/assets/shownotes/regenerate", json={}).status_code == 200
        assert len(starts) == 2


@pytest.mark.parametrize("asset_id", ["", "   "])
def test_empty_review_asset_is_rejected_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asset_id: str
) -> None:
    EpisodeWorkspaceStore(tmp_path).write_episode_yaml({"episode_id": "synthetic"})
    ctx = DashboardContext(workspace=tmp_path)
    starts: list[Any] = []
    monkeypatch.setattr(dashboard_web, "_start_daemon_thread", lambda *args: starts.append(args))
    with TestClient(
        dashboard_web.create_dashboard_app(ctx=ctx, port=8765, session_token=SESSION_TOKEN),
        base_url="http://127.0.0.1:8765",
        headers=SESSION_HEADERS,
    ) as client:
        assert client.post("/api/review", json={"asset_id": asset_id}).status_code == 400
    assert not starts and not ctx.jobs


def test_dashboard_review_of_terminal_protocol_reports_no_agent_calls(tmp_path: Any, monkeypatch: Any) -> None:
    from podcast_pipeline import agent_cli_config, agent_runners
    from podcast_pipeline.agent_runners import FakeCreatorRunner, FakeReviewerRunner
    from podcast_pipeline.dashboard_context import DashboardContext
    from podcast_pipeline.entrypoints import dashboard_web
    from podcast_pipeline.workspace_store import EpisodeWorkspaceStore

    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "synthetic"})
    calls: list[str] = []

    def runners(*, layout: Any, **kwargs: Any) -> Any:
        calls.append("built")
        return (
            FakeCreatorRunner(layout=layout, replies=[{"done": True, "candidate": {"content": "Reviewed"}}]),
            FakeReviewerRunner(layout=layout, reviewer="synthetic", replies=[{"verdict": "ok"}]),
        )

    monkeypatch.setattr(agent_cli_config, "load_agent_cli_bundle", lambda **kwargs: None)
    monkeypatch.setattr(agent_runners, "build_local_cli_runners", runners)
    ctx = DashboardContext(workspace=tmp_path)
    first = ctx.create_job("review")
    dashboard_web._run_review_job(ctx, first, "description", 1)
    assert first.status == "completed" and "note" not in (first.result or {})

    second = ctx.create_job("review")
    dashboard_web._run_review_job(ctx, second, "description", 1)
    assert second.status == "completed", second.error
    assert second.result is not None
    assert second.result["agent_calls"] is False
    assert "no agent calls were made" in second.result["note"]
