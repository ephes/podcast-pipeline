from __future__ import annotations

import asyncio
import io
import json
import math
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from podcast_pipeline.asset_refine_llm import RefineRequest
from podcast_pipeline.dashboard_context import (
    PROGRESS_TRUNCATED,
    BackgroundJob,
    DashboardContext,
    EpisodeMetadataError,
    ProgressEntry,
    SelectedArtifactError,
)
from podcast_pipeline.local_web_guard import (
    RESPONSE_SECURITY_HEADERS,
    SESSION_BOOTSTRAP_PAGE,
    SESSION_HEADER,
    check_request,
    check_session,
    new_session_token,
    open_in_browser,
    session_bootstrap_cookie,
    session_url,
)
from podcast_pipeline.workspace_store import WorkspaceStoreError

_SHUTDOWN_TIMEOUT_SECONDS = 60 * 60  # 1 hour
_DASHBOARD_HTML_PATH = Path(__file__).parent.parent / "static" / "dashboard.html"
_DEFAULT_CANDIDATES = 3
MAX_CANDIDATES = 10
_DEFAULT_REVIEW_ITERATIONS = 3
MAX_REVIEW_ITERATIONS = 10
MAX_JSON_BODY_BYTES = 64 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 10.0
_T = TypeVar("_T")


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate member")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError("Invalid JSON constant")


def _json_depth_allowed(payload: Any) -> bool:
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            if depth >= 64:
                return False
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Nonfinite number")
    return True


def _bounded_int(value: Any, *, default: int, maximum: int) -> int:
    """Coerce a client-supplied count into ``1..maximum`` (``default`` when missing or invalid)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return min(int(value), maximum)


class _LocalRequestGuardMiddleware:
    """Reject DNS-rebinding, cross-origin, non-JSON state-changing and token-less requests.

    The opening ``GET /?token=...`` request sets the session cookie and forwards to ``/``.
    Every HTTP response, rejections included, gets the anti-framing and no-sniff headers.
    """

    def __init__(self, app: ASGIApp, *, expected_port: int | None, session_token: str) -> None:
        self.app = app
        self.expected_port = expected_port
        self.session_token = session_token

    async def __call__(self, scope: Scope, receive: Receive, original_send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, original_send)
            return

        response_started = False

        async def send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                response_headers = MutableHeaders(scope=message)
                for name, value in RESPONSE_SECURITY_HEADERS:
                    response_headers[name] = value
            await original_send(message)

        headers = Headers(scope=scope)
        rejection = check_request(
            method=scope["method"],
            host=headers.get("host"),
            origin=headers.get("origin"),
            sec_fetch_site=headers.get("sec-fetch-site"),
            content_type=headers.get("content-type"),
            expected_port=self.expected_port,
        )
        if rejection is None:
            set_cookie = session_bootstrap_cookie(
                method=scope["method"],
                path=scope["path"],
                query_string=scope.get("query_string", b"").decode("latin-1"),
                token=self.session_token,
                port=self.expected_port,
            )
            if set_cookie is not None:
                bootstrap = HTMLResponse(
                    SESSION_BOOTSTRAP_PAGE,
                    headers={"Set-Cookie": set_cookie, "Cache-Control": "no-store"},
                )
                await bootstrap(scope, receive, send)
                return
            rejection = check_session(
                token=self.session_token,
                cookie_header=headers.get("cookie"),
                header_token=headers.get(SESSION_HEADER),
                port=self.expected_port,
            )
        if rejection is not None:
            response = JSONResponse({"error": rejection.message}, status_code=rejection.status)
            await response(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        except Exception:
            # Answer an unhandled error here, with the security headers, instead of in
            # Starlette's outer error middleware; re-raise so it is still logged.
            if not response_started:
                await PlainTextResponse("Internal Server Error", status_code=500)(scope, receive, send)
            raise


def _to_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _start_daemon_thread(target: Callable[..., Any], *args: Any) -> None:
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()


class _DashboardApi:
    def __init__(
        self,
        *,
        ctx: DashboardContext,
        on_done: Callable[[], None] | None,
    ) -> None:
        self.ctx = ctx
        self.on_done = on_done
        self._shutdown_requested = False

    async def _parse_json_object(self, request: Request) -> tuple[dict[str, Any] | None, Response | None]:
        raw = bytearray()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REQUEST_BODY_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout_at(deadline):
                async for chunk in request.stream():
                    # A ready receive may not yield control to the timeout callback.
                    if loop.time() >= deadline:
                        raise TimeoutError
                    if len(raw) + len(chunk) > MAX_JSON_BODY_BYTES:
                        return None, JSONResponse({"error": "JSON body exceeds 64 KiB limit"}, status_code=413)
                    raw.extend(chunk)
        except TimeoutError:
            return None, JSONResponse({"error": "Request body timed out"}, status_code=408)
        except ClientDisconnect:
            return None, JSONResponse({"error": "Client disconnected"}, status_code=400)
        try:
            payload = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_unique_members, parse_constant=_invalid_constant
            )
            depth_allowed = _json_depth_allowed(payload)
        except (ValueError, UnicodeError, RecursionError):
            return None, JSONResponse({"error": "Invalid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return None, JSONResponse({"error": "Expected JSON object"}, status_code=400)
        if not depth_allowed:
            return None, JSONResponse({"error": "JSON nesting exceeds 64 levels"}, status_code=400)
        return payload, None

    def _locked_call(self, operation: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Run one cohesive storage operation under the context lock, in a worker thread."""
        with self.ctx.lock:
            return operation(*args, **kwargs)

    def _start_job(self, stage: str, target: Callable[..., Any], *args: Any) -> Response:
        """Start ``target`` while the dashboard is open and the stage is available."""
        with self.ctx.lock:
            if self._shutdown_requested:
                return JSONResponse({"error": "Dashboard is shutting down"}, status_code=503)
            running = self.ctx.running_job(stage)
            if running is not None:
                return JSONResponse(
                    {
                        "error": f"A {running.stage} job is already running; conflicts with {stage}",
                        "job_id": running.job_id,
                    },
                    status_code=409,
                )
            job = self.ctx.create_job(stage)
            stderr = sys.stderr
            capture_owner = stderr.capture_owner if isinstance(stderr, _StderrMultiplexer) else None

            def run_job(ctx: DashboardContext, job: BackgroundJob, *args: Any) -> None:
                _thread_local.capture_owner = capture_owner
                try:
                    target(ctx, job, *args)
                finally:
                    del _thread_local.capture_owner
                    with ctx.lock:
                        if job.status == "running":
                            job.error = "Background worker exited unexpectedly"
                            ctx.finish_job(job, "failed")

            try:
                _start_daemon_thread(run_job, self.ctx, job, *args)
            except Exception:
                job.error = "Could not start background worker"
                self.ctx.finish_job(job, "failed")
                return JSONResponse({"error": job.error, "job_id": job.job_id}, status_code=503)
        return JSONResponse({"ok": True, "job_id": job.job_id})

    def serve_html(self, _request: Request) -> Response:
        try:
            html = _DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            return PlainTextResponse(f"Failed to load dashboard.html: {exc}", status_code=500)
        return HTMLResponse(html)

    def serve_status(self, _request: Request) -> Response:
        try:
            return JSONResponse(self.ctx.get_status_json())
        except SelectedArtifactError:
            raise
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    def serve_episode(self, _request: Request) -> Response:
        with self.ctx.lock:
            data = self.ctx.get_episode_json()
            return JSONResponse(data)

    async def handle_update_episode(self, request: Request) -> Response:
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        try:
            error = await run_in_threadpool(self._locked_call, self.ctx.update_episode, payload)
            if error:
                return JSONResponse({"error": error}, status_code=400)
            return JSONResponse({"ok": True})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    def serve_assets(self, _request: Request) -> Response:
        with self.ctx.lock:
            data = self.ctx.get_assets_json()
            return JSONResponse(data)

    async def handle_select(self, request: Request) -> Response:
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        asset_id = payload.get("asset_id")
        candidate_id = payload.get("candidate_id")
        if not isinstance(asset_id, str) or not isinstance(candidate_id, str):
            return JSONResponse({"error": "Missing asset_id or candidate_id"}, status_code=400)

        error = await run_in_threadpool(self._locked_call, self.ctx.select_candidate, asset_id, candidate_id)

        if error:
            return JSONResponse({"error": error}, status_code=400)
        return JSONResponse({"ok": True})

    def handle_delete_candidate(self, request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        candidate_id = request.path_params["candidate_id"]

        with self.ctx.lock:
            error = self.ctx.delete_candidate(asset_id, candidate_id)
        if error:
            return JSONResponse({"error": error}, status_code=400)
        return JSONResponse({"ok": True})

    def serve_asset_notes(self, request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        with self.ctx.lock:
            data = self.ctx.get_asset_notes_json(asset_id)
            return JSONResponse(data)

    def serve_asset_tags(self, request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        with self.ctx.lock:
            data = self.ctx.get_asset_tags_json(asset_id)
            return JSONResponse(data)

    async def handle_put_notes(self, request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        notes = payload.get("notes", "")
        if not isinstance(notes, str):
            return JSONResponse({"error": "notes must be a string"}, status_code=400)

        try:
            await run_in_threadpool(self._locked_call, self.ctx.set_editorial_notes, asset_id, notes)
        except WorkspaceStoreError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    def handle_delete_notes(self, request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        try:
            with self.ctx.lock:
                self.ctx.clear_editorial_notes(asset_id)
        except WorkspaceStoreError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    async def handle_put_tags(self, request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        tags_raw = payload.get("tags")
        if not isinstance(tags_raw, list) or not all(isinstance(tag, str) for tag in tags_raw):
            return JSONResponse({"error": "tags must be a list of strings"}, status_code=400)

        error = await run_in_threadpool(self._locked_call, self.ctx.set_selected_tags, asset_id, tags_raw)
        if error:
            return JSONResponse({"error": error}, status_code=400)
        return JSONResponse({"ok": True})

    async def handle_put_selection(self, request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        text = payload.get("text")
        based_on = payload.get("based_on_candidate_id")
        if not isinstance(text, str):
            return JSONResponse({"error": "text must be a string"}, status_code=400)
        if based_on is not None and not isinstance(based_on, str):
            return JSONResponse({"error": "based_on_candidate_id must be a string"}, status_code=400)

        error = await run_in_threadpool(self._locked_call, self.ctx.select_custom_text, asset_id, text, based_on)
        if error:
            return JSONResponse({"error": error}, status_code=400)
        return JSONResponse({"ok": True})

    async def handle_refine(self, request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        instruction = payload.get("instruction")
        candidate_id = payload.get("candidate_id")
        text = payload.get("text")
        if not isinstance(instruction, str):
            return JSONResponse({"error": "instruction must be a string"}, status_code=400)
        if candidate_id is not None and not isinstance(candidate_id, str):
            return JSONResponse({"error": "candidate_id must be a string"}, status_code=400)
        if text is not None and not isinstance(text, str):
            return JSONResponse({"error": "text must be a string"}, status_code=400)

        prepared = await run_in_threadpool(
            self._locked_call, self.ctx.prepare_refine, asset_id, instruction, candidate_id, text
        )
        if isinstance(prepared, str):
            return JSONResponse({"error": prepared}, status_code=400)
        return await run_in_threadpool(self._start_job, f"refine:{asset_id}", _run_refine_job, prepared)

    async def handle_regenerate(self, request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        candidates_count = _bounded_int(payload.get("candidates"), default=_DEFAULT_CANDIDATES, maximum=MAX_CANDIDATES)
        return await run_in_threadpool(
            self._start_job, f"regenerate:{asset_id}", _run_regenerate_job, asset_id, candidates_count
        )

    async def handle_draft(self, request: Request) -> Response:
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        candidates_count = _bounded_int(payload.get("candidates"), default=_DEFAULT_CANDIDATES, maximum=MAX_CANDIDATES)
        timeout = payload.get("timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            timeout = None

        return await run_in_threadpool(self._start_job, "draft", _run_draft_job, candidates_count, timeout)

    async def handle_draft_summarize(self, request: Request) -> Response:
        _payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response

        return await run_in_threadpool(self._start_job, "summarize", _run_summarize_job)

    async def handle_draft_candidates(self, request: Request) -> Response:
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        candidates_count = _bounded_int(payload.get("candidates"), default=_DEFAULT_CANDIDATES, maximum=MAX_CANDIDATES)
        return await run_in_threadpool(self._start_job, "candidates", _run_candidates_job, candidates_count)

    async def handle_review(self, request: Request) -> Response:
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        asset_id = payload.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id.strip():
            return JSONResponse({"error": "Missing asset_id"}, status_code=400)
        max_iterations = _bounded_int(
            payload.get("max_iterations"), default=_DEFAULT_REVIEW_ITERATIONS, maximum=MAX_REVIEW_ITERATIONS
        )
        return await run_in_threadpool(
            self._start_job, f"review:{asset_id}", _run_review_job, asset_id, max_iterations
        )

    def handle_produce_preview(self, _request: Request) -> Response:
        try:
            from podcast_pipeline.auphonic_payload import build_auphonic_payload

            with self.ctx.lock:
                data = self.ctx.read_episode_metadata() or {}
                payload = build_auphonic_payload(
                    episode_yaml=data,
                    workspace=self.ctx.workspace,
                )
                return JSONResponse(payload)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def handle_produce(self, request: Request) -> Response:
        _payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response

        # Single flight: a second click while a production job runs gets 409
        # instead of starting (and paying for) a second Auphonic production.
        return await run_in_threadpool(self._start_job, "produce", _run_produce_job)

    async def handle_init(self, request: Request) -> Response:
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        episode_id = payload.get("episode_id")
        if not isinstance(episode_id, str) or not episode_id.strip():
            return JSONResponse({"error": "Missing episode_id"}, status_code=400)

        try:
            from podcast_pipeline.entrypoints.init import run_init

            await run_in_threadpool(
                self._locked_call,
                run_init,
                workspace=self.ctx.workspace,
                episode_id=episode_id,
                project_root=self.ctx.workspace.parent,
            )
            return JSONResponse({"ok": True})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def handle_ingest(self, request: Request) -> Response:
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        reaper_dir = payload.get("reaper_media_dir")
        tracks_glob = payload.get("tracks_glob", "*.flac")
        if not isinstance(reaper_dir, str):
            return JSONResponse({"error": "Missing reaper_media_dir"}, status_code=400)

        try:
            from podcast_pipeline.entrypoints.ingest import run_ingest

            await run_in_threadpool(
                self._locked_call,
                run_ingest,
                workspace=self.ctx.workspace,
                reaper_media_dir=Path(reaper_dir),
                tracks_glob=tracks_glob,
            )
            return JSONResponse({"ok": True})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def handle_transcribe(self, request: Request) -> Response:
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        mode = payload.get("mode", "draft")
        if not isinstance(mode, str):
            return JSONResponse({"error": "mode must be a string"}, status_code=400)

        return await run_in_threadpool(self._start_job, "transcribe", _run_transcribe_job, mode)

    def serve_jobs(self, _request: Request) -> Response:
        with self.ctx.lock:
            jobs = [
                {
                    "job_id": j.job_id,
                    "stage": j.stage,
                    "status": j.status,
                    "error": j.error,
                }
                for j in self.ctx.jobs.values()
            ]
            return JSONResponse(jobs)

    def serve_job(self, request: Request) -> Response:
        job_id = request.path_params["job_id"]
        with self.ctx.lock:
            job = self.ctx.jobs.get(job_id)
            if job is None:
                return JSONResponse({"error": f"Job {job_id} not found"}, status_code=404)
            snapshot = {
                "job_id": job.job_id,
                "stage": job.stage,
                "status": job.status,
                "progress": ([PROGRESS_TRUNCATED] if job.progress_start else [])
                + [entry.text for entry in job.progress],
                "error": job.error,
                "result": job.result,
            }
            return JSONResponse(snapshot)

    def _progress_snapshot(
        self, job: BackgroundJob, last_idx: int
    ) -> tuple[int, int, list[tuple[int, str]], str, str | None]:
        with self.ctx.lock:
            first_idx = job.progress_start
            if last_idx > first_idx + len(job.progress):
                last_idx = 0
            progress = [
                (first_idx + index + 1, entry.text)
                for index, entry in enumerate(job.progress)
                if first_idx + index + 1 > last_idx
            ]
            return last_idx, first_idx, progress, job.status, job.error

    async def serve_job_stream(self, request: Request) -> Response:
        job_id = request.path_params["job_id"]
        job = await run_in_threadpool(self._locked_call, self.ctx.jobs.get, job_id)
        if job is None:
            return PlainTextResponse("Job not found", status_code=404)

        async def event_generator() -> Any:
            try:
                last_idx = max(0, int(request.headers.get("last-event-id", "0")))
            except ValueError:
                last_idx = 0
            while True:
                if await request.is_disconnected():
                    break

                last_idx, first_idx, current_progress, status, error = await run_in_threadpool(
                    self._progress_snapshot, job, last_idx
                )

                if last_idx < first_idx:
                    payload = {"type": "progress", "message": PROGRESS_TRUNCATED}
                    yield f"id: {first_idx}\ndata: {json.dumps(payload)}\n\n"
                    last_idx = first_idx
                for entry_id, line in current_progress:
                    payload = {"type": "progress", "message": _to_text(line)}
                    yield f"id: {entry_id}\ndata: {json.dumps(payload)}\n\n"
                    last_idx = entry_id

                if status in ("completed", "failed"):
                    result_data: dict[str, Any] = {"type": "done", "status": status}
                    if error:
                        result_data["error"] = error
                    yield f"data: {json.dumps(result_data)}\n\n"
                    break

                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    def handle_done(self, _request: Request) -> Response:
        with self.ctx.lock:
            if self._shutdown_requested:
                return JSONResponse({"ok": True})
            running = next((job for job in self.ctx.jobs.values() if job.status == "running"), None)
            if running is not None:
                return JSONResponse(
                    {
                        "error": (
                            f"A {running.stage} job is still running. Wait for it to finish, then close the dashboard."
                        ),
                        "job_id": running.job_id,
                    },
                    status_code=409,
                )
            if self.on_done is not None:
                try:
                    _start_daemon_thread(self.on_done)
                except Exception:
                    return JSONResponse({"error": "Could not shut down dashboard. Try again."}, status_code=503)
            self._shutdown_requested = True
            return JSONResponse({"ok": True})


def _episode_metadata_error(_request: Request, exc: Exception) -> Response:
    return JSONResponse({"error": str(exc)}, status_code=400)


def _selected_artifact_error(_request: Request, exc: Exception) -> Response:
    return JSONResponse({"error": str(exc)}, status_code=422)


def create_dashboard_app(
    *,
    ctx: DashboardContext,
    on_done: Callable[[], None] | None = None,
    port: int | None = None,
    session_token: str,
) -> Starlette:
    """Build the dashboard app.

    ``port`` is the port the server listens on; when given, requests whose
    ``Host`` header names another port are rejected. ``session_token`` is this
    run's secret: requests need its cookie (set by ``GET /?token=...``) or the
    ``X-Session-Token`` header.
    """
    api = _DashboardApi(ctx=ctx, on_done=on_done)

    routes = [
        Route("/", api.serve_html, methods=["GET"]),
        Route("/api/status", api.serve_status, methods=["GET"]),
        Route("/api/episode", api.serve_episode, methods=["GET"]),
        Route("/api/episode", api.handle_update_episode, methods=["POST"]),
        Route("/api/assets", api.serve_assets, methods=["GET"]),
        Route("/api/select", api.handle_select, methods=["POST"]),
        Route(
            "/api/assets/{asset_id:str}/candidates/{candidate_id:str}",
            api.handle_delete_candidate,
            methods=["DELETE"],
        ),
        Route("/api/assets/{asset_id:str}/notes", api.serve_asset_notes, methods=["GET"]),
        Route("/api/assets/{asset_id:str}/notes", api.handle_put_notes, methods=["PUT"]),
        Route("/api/assets/{asset_id:str}/notes", api.handle_delete_notes, methods=["DELETE"]),
        Route("/api/assets/{asset_id:str}/tags", api.serve_asset_tags, methods=["GET"]),
        Route("/api/assets/{asset_id:str}/tags", api.handle_put_tags, methods=["PUT"]),
        Route("/api/assets/{asset_id:str}/selection", api.handle_put_selection, methods=["PUT"]),
        Route("/api/assets/{asset_id:str}/regenerate", api.handle_regenerate, methods=["POST"]),
        Route("/api/assets/{asset_id:str}/refine", api.handle_refine, methods=["POST"]),
        Route("/api/draft", api.handle_draft, methods=["POST"]),
        Route("/api/draft/summarize", api.handle_draft_summarize, methods=["POST"]),
        Route("/api/draft/candidates", api.handle_draft_candidates, methods=["POST"]),
        Route("/api/review", api.handle_review, methods=["POST"]),
        Route("/api/produce/preview", api.handle_produce_preview, methods=["POST"]),
        Route("/api/produce", api.handle_produce, methods=["POST"]),
        Route("/api/init", api.handle_init, methods=["POST"]),
        Route("/api/ingest", api.handle_ingest, methods=["POST"]),
        Route("/api/transcribe", api.handle_transcribe, methods=["POST"]),
        Route("/api/jobs", api.serve_jobs, methods=["GET"]),
        Route("/api/jobs/{job_id:str}", api.serve_job, methods=["GET"]),
        Route("/api/jobs/{job_id:str}/stream", api.serve_job_stream, methods=["GET"]),
        Route("/api/done", api.handle_done, methods=["POST"]),
    ]

    return Starlette(
        routes=routes,
        exception_handlers={
            SelectedArtifactError: _selected_artifact_error,
            EpisodeMetadataError: _episode_metadata_error,
        },
        middleware=[Middleware(_LocalRequestGuardMiddleware, expected_port=port, session_token=session_token)],
    )


def _run_uvicorn_server(server: uvicorn.Server, sock: socket.socket) -> None:
    server.run(sockets=[sock])


def run_dashboard(*, workspace: Path) -> None:
    """Launch the pipeline dashboard web UI."""
    workspace = workspace.expanduser().resolve()
    if not workspace.exists():
        print(f"Workspace does not exist: {workspace}", file=sys.stderr)
        raise SystemExit(1)

    ctx = DashboardContext(workspace=workspace)
    with _serve_stderr(), socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        host = str(sock.getsockname()[0])
        port = int(sock.getsockname()[1])
        session_token = new_session_token()
        url = session_url(f"http://{host}:{port}/", session_token)

        holder: dict[str, uvicorn.Server] = {}

        def request_shutdown() -> None:
            server = holder.get("server")
            if server is not None:
                server.should_exit = True

        app = create_dashboard_app(ctx=ctx, on_done=request_shutdown, port=port, session_token=session_token)
        config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            access_log=False,
            log_level="error",
        )
        server = uvicorn.Server(config=config)
        holder["server"] = server

        shutdown_timer = threading.Timer(_SHUTDOWN_TIMEOUT_SECONDS, request_shutdown)
        shutdown_timer.daemon = True
        shutdown_timer.start()

        try:
            print(f"Dashboard: {url}", file=sys.stderr)
            with open_in_browser(url):
                _run_uvicorn_server(server, sock)
        finally:
            shutdown_timer.cancel()

    print("Dashboard closed.", file=sys.stderr)


# --- Background job runners ---

_thread_local = threading.local()
_stderr_lock = threading.Lock()


class _StderrMultiplexer(io.TextIOBase):
    """Thread-aware stderr wrapper that routes writes to per-job captures.

    Job captures are tied to the installation that admitted the job.
    Other writes go to the original stderr.
    """

    def __init__(self, real_stderr: Any) -> None:
        self._real = real_stderr
        self.capture_owner = object()
        self.serve_count = 0

    # TextIOBase API
    def write(self, s: Any) -> int:
        text = _to_text(s)
        target = getattr(_thread_local, "capture", None)
        if target is not None and getattr(_thread_local, "capture_owner", None) is self.capture_owner:
            return int(target.write(text))
        return int(self._real.write(text))

    def flush(self) -> None:
        self._real.flush()

    def fileno(self) -> int:
        return int(self._real.fileno())

    def isatty(self) -> bool:
        return bool(self._real.isatty())


@contextmanager
def _serve_stderr() -> Iterator[None]:
    """Share an installation across overlapping serves and restore it on final exit."""
    with _stderr_lock:
        wrapper = sys.stderr
        if not isinstance(wrapper, _StderrMultiplexer):
            wrapper = _StderrMultiplexer(wrapper)
            sys.stderr = wrapper
        wrapper.serve_count += 1
    try:
        yield
    finally:
        with _stderr_lock:
            wrapper.serve_count -= 1
            if wrapper.serve_count == 0 and sys.stderr is wrapper:
                sys.stderr = wrapper._real


class _ProgressCapture(io.StringIO):
    """Captures typer.echo output and stores it as job progress."""

    def __init__(self, ctx: DashboardContext, job: BackgroundJob) -> None:
        super().__init__()
        self._ctx = ctx
        self._job = job

    def write(self, s: Any) -> int:
        text = _to_text(s)
        entry = ProgressEntry.from_text(text)
        if entry is not None:
            with self._ctx.lock:
                self._job.append_progress(entry)
        return len(text)


def _run_regenerate_job(
    ctx: DashboardContext,
    job: BackgroundJob,
    asset_id: str,
    candidates_count: int,
) -> None:
    try:
        from podcast_pipeline.asset_candidates_llm import generate_single_asset_candidates_llm

        capture = _ProgressCapture(ctx, job)
        _thread_local.capture = capture
        try:
            notes = ctx.get_editorial_notes(asset_id)
            new_candidates = generate_single_asset_candidates_llm(
                workspace=ctx.workspace,
                asset_id=asset_id,
                candidates_per_asset=candidates_count,
                editorial_notes=notes or None,
            )
            for c in new_candidates:
                ctx.store.write_candidate(c)
        finally:
            _thread_local.capture = None

        with ctx.lock:
            ctx.reload_candidates()
            ctx.finish_job(job, "completed")
            job.result = {"candidates": len(new_candidates)}
    except Exception as exc:
        with ctx.lock:
            ctx.finish_job(job, "failed")
            job.error = str(exc)


def _run_refine_job(
    ctx: DashboardContext,
    job: BackgroundJob,
    request: RefineRequest,
) -> None:
    try:
        from podcast_pipeline.asset_refine_llm import refine_asset_text

        capture = _ProgressCapture(ctx, job)
        _thread_local.capture = capture
        try:
            candidate = refine_asset_text(workspace=ctx.workspace, request=request)
        finally:
            _thread_local.capture = None

        with ctx.lock:
            ctx.add_candidate(candidate)
            ctx.finish_job(job, "completed")
            job.result = {"candidate_id": str(candidate.candidate_id)}
    except Exception as exc:
        with ctx.lock:
            ctx.finish_job(job, "failed")
            job.error = str(exc)


def _run_draft_job(
    ctx: DashboardContext,
    job: BackgroundJob,
    candidates_count: int,
    timeout: float | None,
) -> None:
    try:
        from podcast_pipeline.entrypoints.draft_pipeline import _run_llm_pipeline
        from podcast_pipeline.transcript_chunker import ChunkerConfig

        capture = _ProgressCapture(ctx, job)
        _thread_local.capture = capture
        try:
            _run_llm_pipeline(
                store=ctx.store,
                candidates_per_asset=candidates_count,
                chunker_config=ChunkerConfig(),
                timeout_seconds=timeout,
            )
        finally:
            _thread_local.capture = None

        with ctx.lock:
            ctx.reload_candidates()
            ctx.finish_job(job, "completed")
    except Exception as exc:
        with ctx.lock:
            ctx.finish_job(job, "failed")
            job.error = str(exc)


def _run_summarize_job(
    ctx: DashboardContext,
    job: BackgroundJob,
) -> None:
    try:
        from podcast_pipeline.agent_cli_config import load_agent_cli_bundle
        from podcast_pipeline.drafter_runner import DrafterCliRunner
        from podcast_pipeline.entrypoints.draft_pipeline import _discover_chunk_ids
        from podcast_pipeline.prompting import PromptRenderer, default_prompt_registry
        from podcast_pipeline.summarization_llm import run_llm_summarization

        capture = _ProgressCapture(ctx, job)
        _thread_local.capture = capture
        try:
            bundle = load_agent_cli_bundle(workspace=ctx.workspace)
            renderer = PromptRenderer(default_prompt_registry())
            # timeout_seconds=None resolves to the default agent CLI timeout, so a hung
            # CLI fails the job instead of pinning the stage to "running".
            runner = DrafterCliRunner(
                config=bundle.drafter,
                timeout_seconds=None,
                cwd=str(ctx.workspace),
            )
            chunk_ids = _discover_chunk_ids(ctx.store)
            if not chunk_ids:
                raise RuntimeError("No chunks found. Run draft or chunking first.")
            run_llm_summarization(
                layout=ctx.layout,
                chunk_ids=chunk_ids,
                runner=runner,
                renderer=renderer,
                hosts=ctx.store.read_episode_hosts(),
            )
        finally:
            _thread_local.capture = None

        with ctx.lock:
            ctx.finish_job(job, "completed")
    except Exception as exc:
        with ctx.lock:
            ctx.finish_job(job, "failed")
            job.error = str(exc)


def _run_candidates_job(
    ctx: DashboardContext,
    job: BackgroundJob,
    candidates_count: int,
) -> None:
    try:
        from podcast_pipeline.entrypoints.draft_candidates import run_draft_candidates

        capture = _ProgressCapture(ctx, job)
        _thread_local.capture = capture
        try:
            run_draft_candidates(
                workspace=ctx.workspace,
                chapters=None,
                candidates_per_asset=candidates_count,
            )
        finally:
            _thread_local.capture = None

        with ctx.lock:
            ctx.reload_candidates()
            ctx.finish_job(job, "completed")
    except Exception as exc:
        with ctx.lock:
            ctx.finish_job(job, "failed")
            job.error = str(exc)


def _run_review_job(
    ctx: DashboardContext,
    job: BackgroundJob,
    asset_id: str,
    max_iterations: int,
) -> None:
    try:
        from podcast_pipeline.agent_cli_config import load_agent_cli_bundle
        from podcast_pipeline.agent_runners import build_local_cli_runners
        from podcast_pipeline.prompting import PromptRenderer, default_prompt_registry
        from podcast_pipeline.review_loop_engine import load_loop_protocol_state
        from podcast_pipeline.review_loop_orchestrator import run_review_loop_orchestrator
        from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout

        protocol_path = EpisodeWorkspaceLayout(root=ctx.workspace).protocol_state_json_path(asset_id)
        previous = (
            load_loop_protocol_state(protocol_path) if protocol_path.is_symlink() or protocol_path.exists() else None
        )
        capture = _ProgressCapture(ctx, job)
        _thread_local.capture = capture
        try:
            bundle = load_agent_cli_bundle(workspace=ctx.workspace)
            layout = EpisodeWorkspaceLayout(root=ctx.workspace)
            creator, reviewer = build_local_cli_runners(
                layout=layout,
                bundle=bundle,
                renderer=PromptRenderer(default_prompt_registry()),
            )
            protocol_state = run_review_loop_orchestrator(
                workspace=ctx.workspace,
                asset_id=asset_id,
                max_iterations=max_iterations,
                creator=creator,
                reviewer=reviewer,
            )
        finally:
            _thread_local.capture = None

        with ctx.lock:
            ctx.reload_candidates()
            ctx._workspace_state = None  # Force reload
            outcome = protocol_state.decision.outcome if protocol_state.decision else "in_progress"
            ctx.finish_job(job, "completed")
            job.result = {"outcome": outcome}
            if (
                previous is not None
                and previous.decision is not None
                and len(protocol_state.iterations) == len(previous.iterations)
            ):
                # A persisted terminal decision is resumed, never re-reviewed.
                job.result["agent_calls"] = False
                job.result["note"] = (
                    f"Review already finished ({outcome}); no agent calls were made. "
                    f"Archive copy/protocol/{asset_id}/ to start a fresh review."
                )
    except Exception as exc:
        with ctx.lock:
            ctx.finish_job(job, "failed")
            job.error = str(exc)


def _run_produce_job(
    ctx: DashboardContext,
    job: BackgroundJob,
) -> None:
    try:
        from podcast_pipeline.entrypoints.produce import run_produce

        capture = _ProgressCapture(ctx, job)
        _thread_local.capture = capture
        try:
            run_produce(workspace=ctx.workspace, dry_run=False)
        finally:
            _thread_local.capture = None

        with ctx.lock:
            ctx.finish_job(job, "completed")
    except Exception as exc:
        with ctx.lock:
            ctx.finish_job(job, "failed")
            job.error = str(exc)


def _run_transcribe_job(
    ctx: DashboardContext,
    job: BackgroundJob,
    mode: str,
) -> None:
    try:
        from podcast_pipeline.entrypoints.transcribe import TranscribeConfig, TranscriptionMode, run_transcribe

        capture = _ProgressCapture(ctx, job)
        _thread_local.capture = capture
        try:
            resolved_mode = TranscriptionMode(mode.strip().lower())
            config = TranscribeConfig()
            run_transcribe(
                workspace=ctx.workspace,
                mode=resolved_mode,
                config=config,
            )
        finally:
            _thread_local.capture = None

        with ctx.lock:
            ctx.finish_job(job, "completed")
    except Exception as exc:
        with ctx.lock:
            ctx.finish_job(job, "failed")
            job.error = str(exc)
