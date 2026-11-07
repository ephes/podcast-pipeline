from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.testclient import TestClient

from podcast_pipeline import dashboard_context as context
from podcast_pipeline.entrypoints import dashboard_web as web


@pytest.fixture
def ctx(tmp_path: Path) -> context.DashboardContext:
    return context.DashboardContext(workspace=tmp_path)


def test_thread_start_failure_retry(ctx: context.DashboardContext, monkeypatch: pytest.MonkeyPatch) -> None:
    api = web._DashboardApi(ctx=ctx, on_done=None)
    original = threading.Thread.start

    def fail(self: threading.Thread) -> None:
        raise RuntimeError("synthetic start failure")

    monkeypatch.setattr(threading.Thread, "start", fail)
    response = api._start_job("test", lambda *args: None)
    assert response.status_code == 503
    job = next(iter(ctx.jobs.values()))
    assert job.status == "failed"
    assert "synthetic" not in bytes(response.body).decode()
    monkeypatch.setattr(threading.Thread, "start", original)
    monkeypatch.setattr(web, "_start_daemon_thread", lambda *args: None)
    assert api._start_job("test", lambda *args: None).status_code == 200
    assert api._start_job("test", lambda *args: None).status_code == 409


def test_racing_requests_around_launch_failure(
    ctx: context.DashboardContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = web._DashboardApi(ctx=ctx, on_done=None)
    entered, release = threading.Event(), threading.Event()
    attempts: list[int] = []

    def launch(*args: Any) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(5)
            raise RuntimeError("synthetic launch failure")

    monkeypatch.setattr(web, "_start_daemon_thread", launch)
    responses: list[int] = []

    def request() -> None:
        responses.append(api._start_job("test", lambda *args: None).status_code)

    first, second = threading.Thread(target=request), threading.Thread(target=request)
    first.start()
    assert entered.wait(5)
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert sorted(responses) == [200, 503]
    assert sorted(job.status for job in ctx.jobs.values()) == ["failed", "running"]


def test_progress_byte_and_entry_caps(ctx: context.DashboardContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context, "MAX_PROGRESS_BYTES", 64, raising=False)
    monkeypatch.setattr(context, "MAX_PROGRESS_ENTRIES", 3, raising=False)
    job = ctx.create_job("test")
    capture = web._ProgressCapture(ctx, job)
    for i in range(20):
        capture.write(str(i))
    assert list(job.progress) == ["17", "18", "19"]
    assert job.progress_start == 17
    capture.write("é" * 1000)
    assert sum(len(line.encode()) for line in job.progress) <= 64
    assert job.progress_bytes <= 64
    assert job.progress_start >= 20
    with TestClient(web.create_dashboard_app(ctx=ctx), base_url="http://127.0.0.1") as client:
        result = client.get(f"/api/jobs/{job.job_id}").json()
    assert result["progress"][0] == context.PROGRESS_TRUNCATED


def test_completed_job_retention_keeps_running(
    ctx: context.DashboardContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context, "MAX_COMPLETED_JOBS", 3, raising=False)
    from podcast_pipeline.entrypoints import transcribe

    monkeypatch.setattr(transcribe, "run_transcribe", lambda **kwargs: None)
    running = ctx.create_job("running")
    for _ in range(20):
        with ctx.lock:
            job = ctx.create_job("transcribe")
        web._run_transcribe_job(ctx, job, "draft")
    assert len(ctx.jobs) == 4
    assert ctx.jobs[running.job_id] is running
    with ctx.lock:
        ctx.finish_job(running, "completed")
    assert len(ctx.jobs) == 3
    assert ctx.jobs[running.job_id] is running
    assert list(ctx.jobs.values())[-1] is running


def test_sse_live_and_reconnect_after_eviction(
    ctx: context.DashboardContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context, "MAX_PROGRESS_ENTRIES", 2, raising=False)
    job = ctx.create_job("test")
    capture = web._ProgressCapture(ctx, job)
    capture.write("first")

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(60)
        return {"type": "http.disconnect"}

    async def exercise() -> None:
        request = Request({"type": "http", "headers": [], "path_params": {"job_id": job.job_id}}, receive)
        response = await web._DashboardApi(ctx=ctx, on_done=None).serve_job_stream(request)
        assert isinstance(response, StreamingResponse)
        iterator = response.body_iterator.__aiter__()
        assert "id: 1\n" in str(await anext(iterator))
        for message in ["second", "third", "fourth"]:
            capture.write(message)
        with ctx.lock:
            ctx.finish_job(job, "completed")
        remaining = "".join([str(chunk) async for chunk in response.body_iterator])
        assert context.PROGRESS_TRUNCATED in remaining
        assert "id: 3\n" in remaining and "id: 4\n" in remaining
        assert '"message": "second"' not in remaining

    asyncio.run(exercise())
    with TestClient(web.create_dashboard_app(ctx=ctx), base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/jobs/{job.job_id}/stream", headers={"Last-Event-ID": "3"})
    assert "id: 4\n" in response.text
    assert "id: 3\n" not in response.text


@pytest.mark.parametrize("length", [None, "1"])
def test_streamed_json_size_limit(ctx: context.DashboardContext, length: str | None) -> None:
    consumed: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        for i in range(10):
            consumed.append(i)
            yield b" " * 40000

    async def exercise() -> None:
        headers = {"Content-Type": "application/json"}
        if length is not None:
            headers["Content-Length"] = length
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web.create_dashboard_app(ctx=ctx)),
            base_url="http://127.0.0.1",
        ) as client:
            response = await client.post("/api/transcribe", content=body(), headers=headers)
        assert response.status_code == 413

    asyncio.run(exercise())
    assert consumed == [0, 1]
    assert not ctx.jobs
    assert not list(ctx.workspace.iterdir())


@pytest.mark.parametrize(
    "body",
    [b"\xff", b'{"x":' + b"[" * 10000 + b"]" * 10000 + b"}", b"{} trailing"],
    ids=["invalid-utf8", "too-deep", "trailing"],
)
def test_invalid_json_is_controlled(ctx: context.DashboardContext, body: bytes) -> None:
    with TestClient(web.create_dashboard_app(ctx=ctx), base_url="http://127.0.0.1") as client:
        response = client.post("/api/transcribe", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert not ctx.jobs
    assert not list(ctx.workspace.iterdir())


def test_guard_precedes_body_consumption(ctx: context.DashboardContext) -> None:
    consumed: list[bool] = []

    async def body() -> AsyncIterator[bytes]:
        consumed.append(True)
        yield b"{}"

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web.create_dashboard_app(ctx=ctx)),
            base_url="http://foreign.test",
        ) as client:
            response = await client.post(
                "/api/transcribe", content=body(), headers={"Content-Type": "application/json"}
            )
        assert response.status_code == 403

    asyncio.run(exercise())
    assert not consumed
