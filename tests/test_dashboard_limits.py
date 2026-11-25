from __future__ import annotations

import asyncio
import os
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.testclient import TestClient

from podcast_pipeline import dashboard_context as context
from podcast_pipeline.domain.models import Candidate, TextFormat
from podcast_pipeline.entrypoints import dashboard_web as web
from podcast_pipeline.transcript_chunker import ChunkerConfig, write_transcript_chunks
from tests.session_support import SESSION_HEADERS, SESSION_TOKEN


@pytest.fixture
def ctx(tmp_path: Path) -> context.DashboardContext:
    return context.DashboardContext(workspace=tmp_path)


def test_status_retries_rejected_candidate_inventory(ctx: context.DashboardContext) -> None:
    candidate = Candidate(asset_id="description", content="Synthetic description")
    path = ctx.layout.candidate_json_path(candidate.asset_id, candidate.candidate_id)
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")

    api = web._DashboardApi(ctx=ctx, on_done=None)
    request = Request({"type": "http"})
    failed = api.serve_status(request)

    ctx.store.write_candidate(candidate)
    assert ctx.get_status_json()["stages"]["candidates"] == 1
    assert failed.status_code == 500

    assert len(ctx.get_assets_json()) == 1
    path.write_text("{", encoding="utf-8")
    assert api.serve_status(request).status_code == 500
    assert len(ctx.get_assets_json()) == 1


@pytest.mark.parametrize("missing", ["candidate", "output"])
def test_status_requires_selected_candidate_and_output(ctx: context.DashboardContext, missing: str) -> None:
    candidate = Candidate(asset_id="description", content="Synthetic description")
    ctx.store.write_candidate(candidate)
    assert ctx.select_candidate(candidate.asset_id, str(candidate.candidate_id)) is None
    assert ctx.get_status_json()["stages"]["selected"] == 1

    if missing == "candidate":
        ctx.store.delete_candidate_files(candidate.asset_id, candidate.candidate_id)
        ctx.store.write_candidate(candidate.model_copy(update={"asset_id": "shownotes"}))
    else:
        ctx.store.clear_selected_text(candidate.asset_id)

    stages = ctx.get_status_json()["stages"]
    assert stages["selected"] == 0
    assert stages["total_assets"] == (2 if missing == "candidate" else 1)


@pytest.mark.parametrize("missing", ["candidate", "output"])
def test_pick_badge_matches_status_selection(ctx: context.DashboardContext, missing: str) -> None:
    chosen = Candidate(asset_id="description", content="Synthetic description")
    other = Candidate(asset_id="description", content="Other synthetic description")
    ctx.store.write_candidate(chosen)
    ctx.store.write_candidate(other)
    assert ctx.select_candidate(chosen.asset_id, str(chosen.candidate_id)) is None
    assert ctx.get_status_json()["stages"]["selected"] == 1
    assert ctx.get_assets_json()[0]["has_selection"] is True

    if missing == "candidate":
        ctx.store.delete_candidate_files(chosen.asset_id, chosen.candidate_id)
        ctx.reload_candidates()
    else:
        ctx.store.clear_selected_text(chosen.asset_id)

    assert ctx.get_status_json()["stages"]["selected"] == 0
    (asset,) = ctx.get_assets_json()
    assert asset["has_selection"] is False
    assert asset["custom_selected"] is False


def test_status_bounds_chunk_directory_entries(ctx: context.DashboardContext, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = ctx.layout.transcript_chunks_dir
    directory.mkdir(parents=True)
    (directory / "chunk_0001.txt").touch()
    for i in range(40):
        (directory / f"leftover-{i}").touch()
    monkeypatch.setattr(context, "MAX_CHUNK_INVENTORY_ENTRIES", 8, raising=False)
    scandir = os.scandir
    visited = 0

    @contextmanager
    def track_chunk_entries(path: str | Path) -> Iterator[Iterator[os.DirEntry[str]]]:
        def entries(scan: Iterator[os.DirEntry[str]]) -> Iterator[os.DirEntry[str]]:
            nonlocal visited
            for entry in scan:
                visited += 1
                assert visited <= 9
                yield entry

        with scandir(path) as scan:
            yield entries(scan) if Path(path) == directory else scan

    monkeypatch.setattr(os, "scandir", track_chunk_entries)
    assert ctx.get_status_json()["stages"]["chunks"] is None
    assert visited == 9


def test_status_counts_only_regular_legacy_chunks(ctx: context.DashboardContext) -> None:
    directory = ctx.layout.transcript_chunks_dir
    directory.mkdir(parents=True)
    regular = directory / "chunk_0001.txt"
    regular.touch()
    (directory / "chunk_0002.txt").mkdir()
    (directory / "chunk_0003.txt").symlink_to(regular)

    assert ctx.get_status_json()["stages"]["chunks"] == 1


def test_status_counts_published_chunks_from_manifest(ctx: context.DashboardContext) -> None:
    transcript = ctx.layout.transcript_dir / "transcript.txt"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("one two three four", encoding="utf-8")
    chunks = write_transcript_chunks(
        layout=ctx.layout, transcript_path=transcript, config=ChunkerConfig(max_tokens=2, overlap_tokens=0)
    )
    directory = ctx.layout.transcript_chunks_dir
    (directory / "chunk_stray.txt").touch()
    assert ctx.get_status_json()["stages"]["chunks"] == len(chunks)

    (directory / "complete.json").write_text('{"chunk_ids": [1]}', encoding="utf-8")
    assert ctx.get_status_json()["stages"]["chunks"] == len(chunks) + 1


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
    assert [entry.text for entry in job.progress] == ["17", "18", "19"]
    assert job.progress_start == 17
    capture.write("é" * 1000)
    assert sum(len(entry.text.encode()) for entry in job.progress) <= 64
    assert job.progress_bytes <= 64
    assert job.progress_start >= 20
    with TestClient(
        web.create_dashboard_app(ctx=ctx, session_token=SESSION_TOKEN),
        base_url="http://127.0.0.1",
        headers=SESSION_HEADERS,
    ) as client:
        result = client.get(f"/api/jobs/{job.job_id}").json()
    assert result["progress"][0] == context.PROGRESS_TRUNCATED


def test_progress_only_encodes_bounded_tail_before_lock(
    ctx: context.DashboardContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context, "MAX_PROGRESS_BYTES", 65)
    job = ctx.create_job("test")
    capture = web._ProgressCapture(ctx, job)
    capture.write("first")
    text = " " + "é" * 200_000 + " \n"
    encodes: list[tuple[int, bool]] = []

    def trace(frame: Any, event: str, call: Any) -> None:
        if event == "c_call" and getattr(call, "__name__", None) == "encode" and isinstance(call.__self__, str):
            encodes.append((len(call.__self__), ctx.lock.locked()))

    previous_profile = sys.getprofile()
    sys.setprofile(trace)
    try:
        assert capture.write(text) == len(text)
        assert [entry.text for entry in job.progress] == ["é" * 32]
        assert job.progress_bytes == 64
        capture.write("last")
    finally:
        sys.setprofile(previous_profile)

    assert encodes and all(length <= 65 and not locked for length, locked in encodes)
    assert job.progress_bytes == 4
    assert job.progress_start + len(job.progress) == 4


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
    with TestClient(
        web.create_dashboard_app(ctx=ctx, session_token=SESSION_TOKEN),
        base_url="http://127.0.0.1",
        headers=SESSION_HEADERS,
    ) as client:
        response = client.get(f"/api/jobs/{job.job_id}/stream", headers={"Last-Event-ID": "3"})
    assert "id: 4\n" in response.text
    assert "id: 3\n" not in response.text


def test_sse_replays_and_follows_progress_when_reconnect_cursor_is_ahead(ctx: context.DashboardContext) -> None:
    job = ctx.create_job("test")
    capture = web._ProgressCapture(ctx, job)
    capture.write("first")

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(60)
        return {"type": "http.disconnect"}

    async def exercise() -> None:
        request = Request(
            {
                "type": "http",
                "headers": [(b"last-event-id", b"100")],
                "path_params": {"job_id": job.job_id},
            },
            receive,
        )
        response = await web._DashboardApi(ctx=ctx, on_done=None).serve_job_stream(request)
        assert isinstance(response, StreamingResponse)
        iterator = response.body_iterator.__aiter__()
        first = str(await asyncio.wait_for(anext(iterator), timeout=1))
        assert 'id: 1\ndata: {"type": "progress", "message": "first"}' in first
        capture.write("later")
        with ctx.lock:
            ctx.finish_job(job, "completed")
        remaining = "".join([str(chunk) async for chunk in iterator])
        assert 'id: 2\ndata: {"type": "progress", "message": "later"}' in remaining
        assert '"type": "done"' in remaining

    asyncio.run(exercise())


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
            transport=httpx.ASGITransport(app=web.create_dashboard_app(ctx=ctx, session_token=SESSION_TOKEN)),
            base_url="http://127.0.0.1",
            headers=SESSION_HEADERS,
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
    with TestClient(
        web.create_dashboard_app(ctx=ctx, session_token=SESSION_TOKEN),
        base_url="http://127.0.0.1",
        headers=SESSION_HEADERS,
    ) as client:
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
            transport=httpx.ASGITransport(app=web.create_dashboard_app(ctx=ctx, session_token=SESSION_TOKEN)),
            base_url="http://foreign.test",
            headers=SESSION_HEADERS,
        ) as client:
            response = await client.post(
                "/api/transcribe", content=body(), headers={"Content-Type": "application/json"}
            )
        assert response.status_code == 403

    asyncio.run(exercise())
    assert not consumed


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/select", b'{"asset_id":"description","asset_id":"shownotes","candidate_id":"x"}'),
        ("/api/select", b'{"asset_id":"description","candidate_id":"x","candidate_id":"y"}'),
        ("/api/draft", b'{"candidates":1,"candidates":2}'),
        ("/api/episode", b'{"episode_id":"first","episode_id":"second"}'),
        ("/api/episode", b'{"episode_id":"ep","editorial_notes":{"a":"first","a":"second"}}'),
        ("/api/episode", b'{"episode_id":"ep","metadata":[{"x":1,"x":2}]}'),
        ("/api/draft", b'{"timeout":NaN}'),
        ("/api/draft", b'{"timeout":Infinity}'),
        ("/api/draft", b'{"timeout":-Infinity}'),
        ("/api/draft", b'{"timeout":1e999}'),
        ("/api/episode", b'{"episode_id":"ep","metadata":[{"x":NaN}]}'),
    ],
)
def test_ambiguous_json_never_dispatches(
    ctx: context.DashboardContext, monkeypatch: pytest.MonkeyPatch, path: str, body: bytes
) -> None:
    dispatched: list[object] = []
    monkeypatch.setattr(web, "_start_daemon_thread", lambda *args: dispatched.append(args))
    monkeypatch.setattr(ctx, "select_candidate", lambda *args: dispatched.append(args))
    monkeypatch.setattr(ctx, "update_episode", lambda *args: dispatched.append(args))
    with TestClient(
        web.create_dashboard_app(ctx=ctx, session_token=SESSION_TOKEN),
        base_url="http://127.0.0.1",
        headers=SESSION_HEADERS,
    ) as client:
        response = client.post(path, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid JSON"}
    assert not dispatched
    assert not ctx.jobs
    assert not list(ctx.workspace.iterdir())


def test_ordinary_json_still_updates_episode(ctx: context.DashboardContext) -> None:
    with TestClient(
        web.create_dashboard_app(ctx=ctx, session_token=SESSION_TOKEN),
        base_url="http://127.0.0.1",
        headers=SESSION_HEADERS,
    ) as client:
        response = client.post("/api/episode", json={"episode_id": "synthetic", "editorial_notes": {"a": "ä"}})
        assert response.status_code == 200
        assert client.get("/api/episode").json()["editorial_notes"] == {"a": "ä"}


@pytest.mark.parametrize("drip", [False, True], ids=["stall", "drip"])
def test_body_has_one_total_deadline(
    ctx: context.DashboardContext, monkeypatch: pytest.MonkeyPatch, drip: bool
) -> None:
    monkeypatch.setattr(web, "REQUEST_BODY_TIMEOUT_SECONDS", 0.05, raising=False)
    received = 0
    cancelled = False

    async def receive() -> dict[str, Any]:
        nonlocal received, cancelled
        try:
            await asyncio.sleep(0.015 if drip else 60)
        except asyncio.CancelledError:
            cancelled = True
            raise
        received += 1
        return {"type": "http.request", "body": b" ", "more_body": True}

    async def exercise() -> None:
        request = Request({"type": "http", "headers": []}, receive)
        response = await asyncio.wait_for(web._DashboardApi(ctx=ctx, on_done=None).handle_transcribe(request), 1)
        assert response.status_code == 408
        after = received
        await asyncio.sleep(0.03)
        assert received == after

    asyncio.run(exercise())
    assert cancelled
    assert not ctx.jobs
    assert not list(ctx.workspace.iterdir())


def test_body_disconnect_is_controlled(ctx: context.DashboardContext) -> None:
    calls = 0

    async def receive() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"type": "http.disconnect"}

    async def exercise() -> None:
        request = Request({"type": "http", "headers": []}, receive)
        response = await web._DashboardApi(ctx=ctx, on_done=None).handle_transcribe(request)
        assert response.status_code == 400

    asyncio.run(exercise())
    assert calls == 1
    assert not ctx.jobs
    assert not list(ctx.workspace.iterdir())


def test_body_cancellation_propagates(ctx: context.DashboardContext) -> None:
    async def exercise() -> None:
        entered, retired = asyncio.Event(), asyncio.Event()

        async def receive() -> dict[str, Any]:
            entered.set()
            try:
                await asyncio.sleep(60)
                return {"type": "http.disconnect"}
            finally:
                retired.set()

        request = Request({"type": "http", "headers": []}, receive)
        task = asyncio.create_task(web._DashboardApi(ctx=ctx, on_done=None).handle_transcribe(request))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert retired.is_set()

    asyncio.run(exercise())
    assert not ctx.jobs
    assert not list(ctx.workspace.iterdir())


@pytest.mark.parametrize("port", ["9" * 5000, "²", "0", "65536"], ids=["long", "unicode", "zero", "range"])
def test_dashboard_malformed_port_is_rejected_before_body(ctx: context.DashboardContext, port: str) -> None:
    consumed = False
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal consumed
        consumed = True
        return {"type": "http.request", "body": b"{}"}

    async def send(message: Any) -> None:
        sent.append(message)

    async def exercise() -> None:
        app = web.create_dashboard_app(ctx=ctx, session_token=SESSION_TOKEN)
        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/transcribe",
                "headers": [(b"host", f"127.0.0.1:{port}".encode("latin-1")), (b"content-type", b"application/json")],
            },
            receive,
            send,
        )

    asyncio.run(exercise())
    assert sent[0]["status"] == 403
    assert not consumed
    assert not ctx.jobs
    assert not list(ctx.workspace.iterdir())


@pytest.mark.parametrize("scenario", ["disappeared", "oversized"])
def test_selected_tag_artifact_admission(
    ctx: context.DashboardContext, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    ctx.store.write_candidate(Candidate(asset_id="cms_tags", content="Candidate"))
    markdown = ctx.layout.selected_text_path("cms_tags", TextFormat.markdown)
    plain = ctx.layout.selected_text_path("cms_tags", TextFormat.plain)
    html = ctx.layout.selected_text_path("cms_tags", TextFormat.html)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text("- Markdown", encoding="utf-8")
    plain.write_text("- Plain", encoding="utf-8")
    html.write_text("- HTML", encoding="utf-8")

    with TestClient(
        web.create_dashboard_app(ctx=ctx, port=8765, session_token=SESSION_TOKEN),
        base_url="http://127.0.0.1:8765",
        headers=SESSION_HEADERS,
    ) as client:
        tags_url = "/api/assets/cms_tags/tags"
        assert client.get(tags_url).json()["tags"] == ["Markdown"]
        if scenario == "disappeared":
            original_stat = os.stat

            def stat(path: Any, **kwargs: Any) -> os.stat_result:
                result = original_stat(path, **kwargs)
                if path in (markdown, Path(markdown.name)):
                    markdown.unlink()
                return result

            with monkeypatch.context() as patch:
                patch.setattr(os, "stat", stat)
                assert client.get(tags_url).json()["tags"] == ["Plain"]
            plain.unlink()
            assert client.get(tags_url).json()["tags"] == ["HTML"]
            html.unlink()
            assert client.get(tags_url).json()["tags"] == []
        else:
            with markdown.open("wb") as artifact:
                artifact.truncate(8 * 1024 * 1024 + 1)
            for url in (tags_url, "/api/assets", "/api/status"):
                response = client.get(url)
                assert response.status_code == 422
                assert response.json() == {"error": "Selected artifact cms_tags.md is unreadable or invalid."}
