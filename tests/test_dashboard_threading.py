"""Storage barriers must not block the ASGI event loop or shutdown endpoint."""

from __future__ import annotations

import asyncio
import io
import socket
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import httpx
import pytest

from podcast_pipeline.dashboard_context import DashboardContext
from podcast_pipeline.entrypoints import dashboard_web as web
from tests.session_support import SESSION_HEADERS, SESSION_TOKEN


@pytest.mark.parametrize("exit_at", ["bind", "serve", "normal", "replacement"])
def test_dashboard_restores_owned_stderr_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_at: str
) -> None:
    original, replacement = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stderr", original)
    monkeypatch.setattr(web, "open_in_browser", lambda url: nullcontext())

    if exit_at == "bind":

        def fail_bind(sock: socket.socket, address: Any) -> None:
            raise OSError("synthetic bind failure")

        with monkeypatch.context() as bind_patch:
            bind_patch.setattr(socket.socket, "bind", fail_bind)
            with pytest.raises(OSError, match="synthetic bind failure"):
                web.run_dashboard(workspace=tmp_path)
    else:

        def serve(*args: Any) -> None:
            assert sys.stderr is not original
            if exit_at == "replacement":
                monkeypatch.setattr(sys, "stderr", replacement)
            if exit_at == "serve":
                raise RuntimeError("synthetic serve failure")

        monkeypatch.setattr(web, "_run_uvicorn_server", serve)
        if exit_at == "serve":
            with pytest.raises(RuntimeError, match="synthetic serve failure"):
                web.run_dashboard(workspace=tmp_path)
        else:
            web.run_dashboard(workspace=tmp_path)

    assert sys.stderr is (replacement if exit_at == "replacement" else original)
    assert not original.closed
    original.close()

    next_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", next_stderr)
    monkeypatch.setattr(web, "_run_uvicorn_server", lambda *args: print("unrelated", file=sys.stderr))
    web.run_dashboard(workspace=tmp_path)
    assert "\nunrelated\n" in next_stderr.getvalue()
    assert sys.stderr is next_stderr


def test_job_stderr_capture_ends_when_dashboard_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from podcast_pipeline.entrypoints import transcribe

    ctx = DashboardContext(workspace=tmp_path)
    capturing, resume, finished = threading.Event(), threading.Event(), threading.Event()
    original, next_stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stderr", original)
    monkeypatch.setattr(web, "open_in_browser", lambda url: nullcontext())

    def transcribe_job(**kwargs: Any) -> None:
        print("while serving", file=sys.stderr)
        capturing.set()
        assert resume.wait(5)
        print("after restart", file=sys.stderr)

    monkeypatch.setattr(transcribe, "run_transcribe", transcribe_job)
    api = web._DashboardApi(ctx=ctx, on_done=None)

    def run_job(ctx: DashboardContext, job: Any) -> None:
        try:
            web._run_transcribe_job(ctx, job, "draft")
        finally:
            finished.set()

    def first_serve(*args: Any) -> None:
        assert api._start_job("transcribe", run_job).status_code == 200
        assert capturing.wait(5)

    def second_serve(*args: Any) -> None:
        resume.set()
        assert finished.wait(5)

    try:
        monkeypatch.setattr(web, "_run_uvicorn_server", first_serve)
        web.run_dashboard(workspace=tmp_path)
        monkeypatch.setattr(sys, "stderr", next_stderr)
        monkeypatch.setattr(web, "_run_uvicorn_server", second_serve)
        web.run_dashboard(workspace=tmp_path)
    finally:
        resume.set()
        if ctx.jobs:
            assert finished.wait(5)

    job = next(iter(ctx.jobs.values()))
    assert list(job.progress) == ["while serving"]
    assert "\nafter restart\n" in next_stderr.getvalue()
    assert job.status == "completed"


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
            transport=httpx.ASGITransport(
                app=web.create_dashboard_app(ctx=ctx, on_done=shutdown.set, session_token=SESSION_TOKEN)
            ),
            base_url="http://127.0.0.1",
            headers=SESSION_HEADERS,
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
            transport=httpx.ASGITransport(app=web.create_dashboard_app(ctx=ctx, session_token=SESSION_TOKEN)),
            base_url="http://127.0.0.1",
            headers=SESSION_HEADERS,
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
