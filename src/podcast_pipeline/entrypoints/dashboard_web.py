from __future__ import annotations

import asyncio
import io
import json
import socket
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from podcast_pipeline.dashboard_context import BackgroundJob, DashboardContext

_SHUTDOWN_TIMEOUT_SECONDS = 60 * 60  # 1 hour
_DASHBOARD_HTML_PATH = Path(__file__).parent.parent / "static" / "dashboard.html"


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

    async def _parse_json_object(self, request: Request) -> tuple[dict[str, Any] | None, Response | None]:
        raw = await request.body()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None, JSONResponse({"error": "Invalid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return None, JSONResponse({"error": "Expected JSON object"}, status_code=400)
        return payload, None

    async def serve_html(self, _request: Request) -> Response:
        try:
            html = _DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            return PlainTextResponse(f"Failed to load dashboard.html: {exc}", status_code=500)
        return HTMLResponse(html)

    async def serve_status(self, _request: Request) -> Response:
        try:
            with self.ctx.lock:
                data = self.ctx.get_status_json()
            return JSONResponse(data)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def serve_episode(self, _request: Request) -> Response:
        with self.ctx.lock:
            data = self.ctx.get_episode_json()
        return JSONResponse(data)

    async def handle_update_episode(self, request: Request) -> Response:
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        try:
            with self.ctx.lock:
                error = self.ctx.update_episode(payload)
            if error:
                return JSONResponse({"error": error}, status_code=400)
            return JSONResponse({"ok": True})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def serve_assets(self, _request: Request) -> Response:
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

        with self.ctx.lock:
            error = self.ctx.select_candidate(asset_id, candidate_id)

        if error:
            return JSONResponse({"error": error}, status_code=400)
        return JSONResponse({"ok": True})

    async def serve_asset_notes(self, request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        with self.ctx.lock:
            notes = self.ctx.get_editorial_notes(asset_id)
        return JSONResponse({"asset_id": asset_id, "notes": notes})

    async def handle_put_notes(self, request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        notes = payload.get("notes", "")
        if not isinstance(notes, str):
            return JSONResponse({"error": "notes must be a string"}, status_code=400)

        with self.ctx.lock:
            self.ctx.set_editorial_notes(asset_id, notes)
        return JSONResponse({"ok": True})

    async def handle_delete_notes(self, request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        with self.ctx.lock:
            self.ctx.clear_editorial_notes(asset_id)
        return JSONResponse({"ok": True})

    async def handle_regenerate(self, request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        candidates_count = payload.get("candidates", 3)
        if not isinstance(candidates_count, int) or candidates_count < 1:
            candidates_count = 3

        with self.ctx.lock:
            job = self.ctx.create_job(f"regenerate:{asset_id}")

        _start_daemon_thread(_run_regenerate_job, self.ctx, job, asset_id, candidates_count)
        return JSONResponse({"ok": True, "job_id": job.job_id})

    async def handle_draft(self, request: Request) -> Response:
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        candidates_count = payload.get("candidates", 3)
        timeout = payload.get("timeout")

        with self.ctx.lock:
            job = self.ctx.create_job("draft")

        _start_daemon_thread(_run_draft_job, self.ctx, job, candidates_count, timeout)
        return JSONResponse({"ok": True, "job_id": job.job_id})

    async def handle_draft_summarize(self, request: Request) -> Response:
        _payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response

        with self.ctx.lock:
            job = self.ctx.create_job("summarize")

        _start_daemon_thread(_run_summarize_job, self.ctx, job)
        return JSONResponse({"ok": True, "job_id": job.job_id})

    async def handle_draft_candidates(self, request: Request) -> Response:
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        candidates_count = payload.get("candidates", 3)

        with self.ctx.lock:
            job = self.ctx.create_job("candidates")

        _start_daemon_thread(_run_candidates_job, self.ctx, job, candidates_count)
        return JSONResponse({"ok": True, "job_id": job.job_id})

    async def handle_review(self, request: Request) -> Response:
        payload, error_response = await self._parse_json_object(request)
        if error_response is not None:
            return error_response
        assert payload is not None

        asset_id = payload.get("asset_id")
        max_iterations = payload.get("max_iterations", 3)
        if not isinstance(asset_id, str):
            return JSONResponse({"error": "Missing asset_id"}, status_code=400)

        with self.ctx.lock:
            job = self.ctx.create_job(f"review:{asset_id}")

        _start_daemon_thread(_run_review_job, self.ctx, job, asset_id, max_iterations)
        return JSONResponse({"ok": True, "job_id": job.job_id})

    async def handle_produce_preview(self, _request: Request) -> Response:
        try:
            from podcast_pipeline.auphonic_payload import build_auphonic_payload

            with self.ctx.lock:
                data = self.ctx._read_episode_yaml_safe()
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

        with self.ctx.lock:
            job = self.ctx.create_job("produce")

        _start_daemon_thread(_run_produce_job, self.ctx, job)
        return JSONResponse({"ok": True, "job_id": job.job_id})

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

            run_init(
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

            run_ingest(
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

        with self.ctx.lock:
            job = self.ctx.create_job("transcribe")

        _start_daemon_thread(_run_transcribe_job, self.ctx, job, mode)
        return JSONResponse({"ok": True, "job_id": job.job_id})

    async def serve_jobs(self, _request: Request) -> Response:
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

    async def serve_job(self, request: Request) -> Response:
        job_id = request.path_params["job_id"]
        with self.ctx.lock:
            job = self.ctx.jobs.get(job_id)
            if job is None:
                return JSONResponse({"error": f"Job {job_id} not found"}, status_code=404)
            snapshot = {
                "job_id": job.job_id,
                "stage": job.stage,
                "status": job.status,
                "progress": [_to_text(line) for line in job.progress],
                "error": job.error,
                "result": job.result,
            }
        return JSONResponse(snapshot)

    async def serve_job_stream(self, request: Request) -> Response:
        job_id = request.path_params["job_id"]
        with self.ctx.lock:
            job = self.ctx.jobs.get(job_id)
        if job is None:
            return PlainTextResponse("Job not found", status_code=404)

        async def event_generator() -> Any:
            last_idx = 0
            while True:
                if await request.is_disconnected():
                    break

                with self.ctx.lock:
                    current_progress = list(job.progress)
                    status = job.status
                    error = job.error

                if len(current_progress) > last_idx:
                    for line in current_progress[last_idx:]:
                        payload = {"type": "progress", "message": _to_text(line)}
                        yield f"data: {json.dumps(payload)}\n\n"
                    last_idx = len(current_progress)

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

    async def handle_done(self, _request: Request) -> Response:
        if self.on_done is not None:
            _start_daemon_thread(self.on_done)
        return JSONResponse({"ok": True})


def create_dashboard_app(
    *,
    ctx: DashboardContext,
    on_done: Callable[[], None] | None = None,
) -> Starlette:
    api = _DashboardApi(ctx=ctx, on_done=on_done)

    routes = [
        Route("/", api.serve_html, methods=["GET"]),
        Route("/api/status", api.serve_status, methods=["GET"]),
        Route("/api/episode", api.serve_episode, methods=["GET"]),
        Route("/api/episode", api.handle_update_episode, methods=["POST"]),
        Route("/api/assets", api.serve_assets, methods=["GET"]),
        Route("/api/select", api.handle_select, methods=["POST"]),
        Route("/api/assets/{asset_id:str}/notes", api.serve_asset_notes, methods=["GET"]),
        Route("/api/assets/{asset_id:str}/notes", api.handle_put_notes, methods=["PUT"]),
        Route("/api/assets/{asset_id:str}/notes", api.handle_delete_notes, methods=["DELETE"]),
        Route("/api/assets/{asset_id:str}/regenerate", api.handle_regenerate, methods=["POST"]),
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

    return Starlette(routes=routes)


def _run_uvicorn_server(server: uvicorn.Server, sock: socket.socket) -> None:
    server.run(sockets=[sock])


def run_dashboard(*, workspace: Path) -> None:
    """Launch the pipeline dashboard web UI."""
    workspace = workspace.expanduser().resolve()
    if not workspace.exists():
        print(f"Workspace does not exist: {workspace}", file=sys.stderr)
        raise SystemExit(1)

    ctx = DashboardContext(workspace=workspace)
    _install_stderr_multiplexer()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        host = str(sock.getsockname()[0])
        port = int(sock.getsockname()[1])
        url = f"http://{host}:{port}/"

        holder: dict[str, uvicorn.Server] = {}

        def request_shutdown() -> None:
            server = holder.get("server")
            if server is not None:
                server.should_exit = True

        app = create_dashboard_app(ctx=ctx, on_done=request_shutdown)
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

        print(f"Dashboard: {url}", file=sys.stderr)
        webbrowser.open(url)

        try:
            _run_uvicorn_server(server, sock)
        finally:
            shutdown_timer.cancel()

    print("Dashboard closed.", file=sys.stderr)


# --- Background job runners ---

_thread_local = threading.local()


class _StderrMultiplexer(io.TextIOBase):
    """Thread-aware stderr wrapper that routes writes to per-job captures.

    Installed once on sys.stderr.  When a background job thread sets
    ``_thread_local.capture``, writes from that thread go to the capture
    instead of the real stderr.  All other threads (including the main
    thread) see the original stderr unchanged.
    """

    def __init__(self, real_stderr: Any) -> None:
        self._real = real_stderr

    # TextIOBase API
    def write(self, s: Any) -> int:
        text = _to_text(s)
        target = getattr(_thread_local, "capture", None)
        if target is not None:
            return int(target.write(text))
        return int(self._real.write(text))

    def flush(self) -> None:
        self._real.flush()

    def fileno(self) -> int:
        return int(self._real.fileno())

    def isatty(self) -> bool:
        return bool(self._real.isatty())


def _install_stderr_multiplexer() -> None:
    """Install the multiplexer exactly once (idempotent)."""
    if not isinstance(sys.stderr, _StderrMultiplexer):
        sys.stderr = _StderrMultiplexer(sys.stderr)


class _ProgressCapture(io.StringIO):
    """Captures typer.echo output and stores it as job progress."""

    def __init__(self, ctx: DashboardContext, job: BackgroundJob) -> None:
        super().__init__()
        self._ctx = ctx
        self._job = job

    def write(self, s: Any) -> int:
        text = _to_text(s)
        if text.strip():
            with self._ctx.lock:
                self._job.progress.append(text.strip())
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
            job.status = "completed"
            job.result = {"candidates": len(new_candidates)}
    except Exception as exc:
        with ctx.lock:
            job.status = "failed"
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
            job.status = "completed"
    except Exception as exc:
        with ctx.lock:
            job.status = "failed"
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
            )
        finally:
            _thread_local.capture = None

        with ctx.lock:
            job.status = "completed"
    except Exception as exc:
        with ctx.lock:
            job.status = "failed"
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
            job.status = "completed"
    except Exception as exc:
        with ctx.lock:
            job.status = "failed"
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
        from podcast_pipeline.review_loop_orchestrator import run_review_loop_orchestrator
        from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout

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
            job.status = "completed"
            job.result = {"outcome": outcome}
    except Exception as exc:
        with ctx.lock:
            job.status = "failed"
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
            job.status = "completed"
    except Exception as exc:
        with ctx.lock:
            job.status = "failed"
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
            job.status = "completed"
    except Exception as exc:
        with ctx.lock:
            job.status = "failed"
            job.error = str(exc)
