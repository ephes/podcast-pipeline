import threading
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from podcast_pipeline.dashboard_context import DashboardContext
from podcast_pipeline.domain.models import Candidate
from podcast_pipeline.entrypoints import dashboard_web
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


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
        dashboard_web.create_dashboard_app(ctx=ctx, port=8765), base_url="http://127.0.0.1:8765"
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


def test_different_assets_remain_independent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    EpisodeWorkspaceStore(tmp_path).write_episode_yaml({"episode_id": "synthetic"})
    ctx = DashboardContext(workspace=tmp_path)
    starts = []
    monkeypatch.setattr(dashboard_web, "_start_daemon_thread", lambda *args: starts.append(args))
    with TestClient(
        dashboard_web.create_dashboard_app(ctx=ctx, port=8765), base_url="http://127.0.0.1:8765"
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
        dashboard_web.create_dashboard_app(ctx=ctx, port=8765), base_url="http://127.0.0.1:8765"
    ) as client:
        assert client.post("/api/review", json={"asset_id": asset_id}).status_code == 400
    assert not starts and not ctx.jobs
