"""Storage barriers must not block the ASGI event loop or shutdown endpoint."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from podcast_pipeline.dashboard_context import DashboardContext
from podcast_pipeline.entrypoints import dashboard_web as web


@pytest.mark.parametrize(
    ("method", "path", "operation", "result"),
    [
        ("GET", "/api/status", "get_status_json", {"episode_id": "synthetic"}),
        ("GET", "/api/assets", "get_assets_json", []),
        ("POST", "/api/episode", "update_episode", None),
    ],
)
def test_slow_storage_allows_other_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str, operation: str, result: Any
) -> None:
    ctx = DashboardContext(workspace=tmp_path)
    entered, release, shutdown = threading.Event(), threading.Event(), threading.Event()

    def slow(*args: Any) -> Any:
        assert ctx.lock.locked()
        entered.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(ctx, operation, slow)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web.create_dashboard_app(ctx=ctx, on_done=shutdown.set)),
            base_url="http://127.0.0.1",
        ) as client:
            # An external watchdog makes the old blocking implementation fail without hanging pytest.
            watchdog = threading.Timer(2, release.set)
            watchdog.start()
            started = time.monotonic()
            pending = asyncio.create_task(client.request(method, path, json={"episode_id": "synthetic"}))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                assert (await asyncio.wait_for(client.get("/"), 0.5)).status_code == 200
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(client.get("/api/status"), 0.05)
                assert (await asyncio.wait_for(client.post("/api/done", json={}), 0.5)).status_code == 200
                assert time.monotonic() - started < 1
                assert not release.is_set()
            finally:
                release.set()
                watchdog.cancel()
                response = await pending
                watchdog.join()
            assert response.status_code == 200
            assert response.json() == (result if method == "GET" else {"ok": True})
            assert await asyncio.to_thread(shutdown.wait, 1)

    asyncio.run(exercise())


@pytest.mark.parametrize("path", ["/api/status", "/api/jobs", "/api/produce", "/api/jobs/test/stream"])
def test_worker_held_lock_keeps_event_loop_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(web, "_start_daemon_thread", lambda *args: None)
    ctx = DashboardContext(workspace=tmp_path)
    job = ctx.create_job("test")
    ctx.finish_job(job, "completed")
    path = path.replace("/test/", f"/{job.job_id}/")
    release, entered = threading.Event(), threading.Event()

    def hold() -> None:
        with ctx.lock:
            entered.set()
            release.wait(3)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(1)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web.create_dashboard_app(ctx=ctx)), base_url="http://127.0.0.1"
        ) as client:
            watchdog = threading.Timer(2, release.set)
            watchdog.start()
            started = time.monotonic()
            pending = asyncio.create_task(client.post(path, json={}) if path == "/api/produce" else client.get(path))
            try:
                await asyncio.sleep(0.05)
                assert (await asyncio.wait_for(client.post("/api/done", json={}), 0.5)).status_code == 200
                assert time.monotonic() - started < 1
                assert not pending.done()
            finally:
                release.set()
                watchdog.cancel()
                assert (await pending).status_code == 200
                watchdog.join()

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()


def test_sse_poll_waits_for_lock_off_event_loop(tmp_path: Path) -> None:
    from starlette.requests import Request
    from starlette.responses import StreamingResponse

    ctx = DashboardContext(workspace=tmp_path)
    job = ctx.create_job("synthetic")
    job.append_progress("first")
    api = web._DashboardApi(ctx=ctx, on_done=None)

    async def exercise() -> None:
        async def receive() -> dict[str, Any]:
            await asyncio.sleep(60)
            return {"type": "http.disconnect"}

        request = Request({"type": "http", "headers": [], "path_params": {"job_id": job.job_id}}, receive)
        response = await api.serve_job_stream(request)
        assert isinstance(response, StreamingResponse)
        iterator = response.body_iterator.__aiter__()
        assert "first" in str(await anext(iterator))
        entered, release = threading.Event(), threading.Event()

        def finish() -> None:
            with ctx.lock:
                entered.set()
                release.wait(3)
                job.append_progress("last")
                ctx.finish_job(job, "completed")

        worker = threading.Thread(target=finish)
        worker.start()
        assert await asyncio.to_thread(entered.wait, 1)
        watchdog = threading.Timer(2, release.set)
        watchdog.start()
        started = time.monotonic()
        pending = asyncio.ensure_future(anext(iterator))
        try:
            # The stream polls every 0.5 seconds; inspect it while its next snapshot waits for the lock.
            await asyncio.sleep(0.6)
            assert not pending.done()
            assert (await api.handle_done(request)).status_code == 200
            assert time.monotonic() - started < 1.5
        finally:
            release.set()
            watchdog.cancel()
            final = await pending
            worker.join(3)
            watchdog.join()
        assert "last" in str(final)
        assert "completed" in "".join([str(chunk) async for chunk in iterator])
        assert not worker.is_alive()

    asyncio.run(exercise())
