from __future__ import annotations

import json
import sys
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

from podcast_pipeline import asset_refine_llm
from podcast_pipeline.asset_refine_llm import RefineRequest, build_refine_request
from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace, TextFormat
from podcast_pipeline.local_web_guard import check_request
from podcast_pipeline.markdown_html import markdown_to_deterministic_html
from podcast_pipeline.pick_core import (
    build_asset,
    clear_manual_selection_provenance,
    custom_selection_format,
    find_candidate_by_id,
    load_candidates,
    load_workspace,
    read_current_selection,
    save_custom_selection,
    tag_pool_json,
    update_workspace_assets,
    validate_asset_id,
    write_selected_tags,
)
from podcast_pipeline.tag_assets import (
    TAG_ASSET_IDS,
    load_episode_tag_settings,
    render_selected_tags_markdown,
    tag_cap_error,
)
from podcast_pipeline.tag_parsing import parse_tag_list
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore

_SHUTDOWN_TIMEOUT_SECONDS = 30 * 60  # 30 minutes


def run_pick_web(*, workspace: Path, asset_id: str | None) -> None:
    """Launch a local web UI for picking candidates."""
    store = EpisodeWorkspaceStore(workspace)
    layout = store.layout
    try:
        candidates_by_asset = load_candidates(layout=layout, asset_id=asset_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not candidates_by_asset:
        print(f"No candidates found under {layout.copy_candidates_dir}", file=sys.stderr)
        raise SystemExit(1)

    workspace_state = load_workspace(store)

    ctx = _ServerContext(
        store=store,
        candidates_by_asset=candidates_by_asset,
        workspace_state=workspace_state,
    )

    handler = partial(_PickWebHandler, ctx)
    # Threaded, so a long-running refine request does not block the rest of the UI.
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    host = str(server.server_address[0])
    port = int(server.server_address[1])
    url = f"http://{host}:{port}/"

    # Hard timeout fallback
    shutdown_timer = threading.Timer(_SHUTDOWN_TIMEOUT_SECONDS, server.shutdown)
    shutdown_timer.daemon = True
    shutdown_timer.start()

    print(f"Pick UI: {url}", file=sys.stderr)
    webbrowser.open(url)

    try:
        server.serve_forever()
    finally:
        shutdown_timer.cancel()
        server.server_close()

    print("Pick UI closed.", file=sys.stderr)


class _ServerContext:
    """Shared mutable state for the pick web server."""

    def __init__(
        self,
        *,
        store: EpisodeWorkspaceStore,
        candidates_by_asset: dict[str, list[Candidate]],
        workspace_state: EpisodeWorkspace,
    ) -> None:
        self.store = store
        self.candidates_by_asset = candidates_by_asset
        self.workspace_state: EpisodeWorkspace = workspace_state
        self.tag_settings, self.hosts = load_episode_tag_settings(store.layout.root)
        self.lock = threading.Lock()
        self.refining: set[str] = set()

    def get_selected_tags(self, asset_id: str) -> list[str]:
        for fmt in (TextFormat.markdown, TextFormat.plain, TextFormat.html):
            path = self.store.layout.selected_text_path(asset_id, fmt)
            if path.exists():
                return parse_tag_list(path.read_text(encoding="utf-8"))
        return []

    def set_selected_tags(self, asset_id: str, tags: list[str]) -> str | None:
        """Write a curated tag selection. Returns error message on failure, None on success."""
        try:
            validate_asset_id(asset_id)
        except ValueError as exc:
            return str(exc)
        if asset_id not in TAG_ASSET_IDS or asset_id not in self.candidates_by_asset:
            return f"Asset {asset_id} does not support per-tag editing"
        self.workspace_state, error = write_selected_tags(
            store=self.store,
            workspace_state=self.workspace_state,
            asset_id=asset_id,
            tags=tags,
            max_tags=self.tag_settings.max_tags,
        )
        return error

    def get_assets_json(self) -> list[dict[str, object]]:
        ws = self.workspace_state
        assets_by_id = {asset.asset_id: asset for asset in ws.assets}

        result: list[dict[str, object]] = []
        for asset_key in sorted(self.candidates_by_asset):
            candidates = self.candidates_by_asset[asset_key]
            existing = assets_by_id.get(asset_key)
            selected_id = str(existing.selected_candidate_id) if existing and existing.selected_candidate_id else None
            current = read_current_selection(self.store.layout, asset_key)
            custom_selected = selected_id is None and current is not None and bool(current[1].strip())

            candidate_items: list[dict[str, object]] = []
            for c in candidates:
                candidate_items.append(
                    {
                        "candidate_id": str(c.candidate_id),
                        "content": c.content,
                        "content_html": markdown_to_deterministic_html(c.content),
                        "format": c.format.value,
                        "instruction": asset_refine_llm.refine_instruction(c),
                    }
                )

            asset_data: dict[str, object] = {
                "asset_id": asset_key,
                "selected_candidate_id": selected_id,
                "has_selection": selected_id is not None or custom_selected,
                "custom_selected": custom_selected,
                "selected_text": current[1] if custom_selected and current is not None else None,
                "selection_format": custom_selection_format(asset_key).value,
                "candidates": candidate_items,
                "is_tag_asset": asset_key in TAG_ASSET_IDS,
            }
            if asset_key in TAG_ASSET_IDS:
                asset_data["tag_pool"] = tag_pool_json(candidates, settings=self.tag_settings, hosts=self.hosts)
                asset_data["selected_tags"] = self.get_selected_tags(asset_key)
                asset_data["max_tags"] = self.tag_settings.max_tags
            result.append(asset_data)
        return result

    def select_candidate(self, asset_id: str, candidate_id_str: str) -> str | None:
        """Select a candidate. Returns error message on failure, None on success."""
        try:
            validate_asset_id(asset_id)
        except ValueError as exc:
            return str(exc)

        if asset_id not in self.candidates_by_asset:
            return f"Unknown asset_id: {asset_id}"

        try:
            candidate_uuid = UUID(candidate_id_str)
        except ValueError:
            return f"Invalid candidate_id: {candidate_id_str}"

        candidates = self.candidates_by_asset[asset_id]
        match = find_candidate_by_id(candidates, candidate_uuid)
        if match is None:
            return f"candidate_id {candidate_id_str} not found for asset {asset_id}"

        ws = self.workspace_state
        assets_by_id = {asset.asset_id: asset for asset in ws.assets}
        existing = assets_by_id.get(asset_id)

        if asset_id in TAG_ASSET_IDS:
            selected_tags = parse_tag_list(match.content)
            error = tag_cap_error(asset_id, len(selected_tags), self.tag_settings.max_tags)
            if error is not None:
                return error
            content = render_selected_tags_markdown(asset_id, selected_tags)
            self.store.write_selected_text(asset_id, TextFormat.markdown, content)
        else:
            self.store.write_selected_text(asset_id, match.format, match.content)
        assets_by_id[asset_id] = build_asset(
            asset_id=asset_id,
            existing=existing,
            candidates=candidates,
            selected_candidate_id=match.candidate_id,
        )
        self.workspace_state = update_workspace_assets(clear_manual_selection_provenance(ws, asset_id), assets_by_id)
        self.store.write_state(self.workspace_state)
        return None

    def select_custom_text(self, asset_id: str, text: str, based_on_candidate_id: str | None) -> str | None:
        """Use producer-written text as the selection. Returns error message on failure, None on success."""
        try:
            validate_asset_id(asset_id)
        except ValueError as exc:
            return str(exc)

        if asset_id not in self.candidates_by_asset:
            return f"Unknown asset_id: {asset_id}"
        candidates = self.candidates_by_asset[asset_id]

        based_on: Candidate | None = None
        if based_on_candidate_id is not None:
            try:
                based_on_uuid = UUID(based_on_candidate_id)
            except ValueError:
                return f"Invalid based_on_candidate_id: {based_on_candidate_id}"
            based_on = find_candidate_by_id(candidates, based_on_uuid)
            if based_on is None:
                return f"candidate_id {based_on_candidate_id} not found for asset {asset_id}"

        try:
            self.workspace_state = save_custom_selection(
                store=self.store,
                workspace=self.workspace_state,
                asset_id=asset_id,
                candidates=candidates,
                text=text,
                based_on=based_on,
            )
        except ValueError as exc:
            return str(exc)
        return None

    def prepare_refine(
        self,
        asset_id: str,
        instruction: str,
        candidate_id: str | None,
        text: str | None,
    ) -> RefineRequest | str:
        """Validate a refine request. Returns the request, or an error message."""
        try:
            validate_asset_id(asset_id)
        except ValueError as exc:
            return str(exc)
        if asset_id not in self.candidates_by_asset:
            return f"Unknown asset_id: {asset_id}"
        current = read_current_selection(self.store.layout, asset_id)
        try:
            return build_refine_request(
                asset_id=asset_id,
                instruction=instruction,
                candidates=self.candidates_by_asset[asset_id],
                candidate_id=candidate_id,
                text=text,
                current_selection_text=current[1] if current is not None else None,
                editorial_notes=self._editorial_notes(asset_id),
            )
        except ValueError as exc:
            return str(exc)

    def add_candidate(self, candidate: Candidate) -> None:
        self.store.write_candidate(candidate)
        self.candidates_by_asset[candidate.asset_id] = [*self.candidates_by_asset[candidate.asset_id], candidate]

    def _editorial_notes(self, asset_id: str) -> str | None:
        if not self.store.layout.episode_yaml.exists():
            return None
        try:
            notes = self.store.read_episode_yaml().get("editorial_notes")
        except Exception:
            return None
        value = notes.get(asset_id) if isinstance(notes, dict) else None
        return value if isinstance(value, str) and value.strip() else None


class _PickWebHandler(BaseHTTPRequestHandler):
    def __init__(self, ctx: _ServerContext, *args: object, **kwargs: object) -> None:
        self.ctx = ctx
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Suppress default stderr logging
        pass

    def _reject_unsafe_request(self) -> bool:
        """Apply the Host/Origin/Content-Type guards; respond and return True when rejected."""
        address = self.server.server_address
        rejection = check_request(
            method=self.command,
            host=self.headers.get("Host"),
            origin=self.headers.get("Origin"),
            sec_fetch_site=self.headers.get("Sec-Fetch-Site"),
            content_type=self.headers.get("Content-Type"),
            expected_port=int(address[1]) if isinstance(address, tuple) else None,
        )
        if rejection is None:
            return False
        self.close_connection = True
        self._respond(rejection.status, "application/json", json.dumps({"error": rejection.message}).encode())
        return True

    def do_GET(self) -> None:
        if self._reject_unsafe_request():
            return
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/assets":
            self._serve_assets_json()
        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self) -> None:
        if self._reject_unsafe_request():
            return
        if self.path == "/api/select":
            self._handle_select()
        elif self.path == "/api/tags":
            self._handle_tags()
        elif self.path == "/api/select-custom":
            self._handle_select_custom()
        elif self.path == "/api/refine":
            self._handle_refine()
        elif self.path == "/api/done":
            self._handle_done()
        else:
            self._respond(404, "text/plain", b"Not found")

    def _serve_html(self) -> None:
        html = _build_html_page()
        self._respond(200, "text/html; charset=utf-8", html.encode("utf-8"))

    def _serve_assets_json(self) -> None:
        with self.ctx.lock:
            data = self.ctx.get_assets_json()
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._respond(200, "application/json", body)

    def _handle_select(self) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, "application/json", json.dumps({"error": "Invalid JSON"}).encode())
            return

        asset_id = payload.get("asset_id")
        candidate_id = payload.get("candidate_id")
        if not isinstance(asset_id, str) or not isinstance(candidate_id, str):
            self._respond(400, "application/json", json.dumps({"error": "Missing asset_id or candidate_id"}).encode())
            return

        with self.ctx.lock:
            error = self.ctx.select_candidate(asset_id, candidate_id)

        if error:
            self._respond(400, "application/json", json.dumps({"error": error}).encode())
        else:
            self._respond(200, "application/json", json.dumps({"ok": True}).encode())

    def _handle_tags(self) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, "application/json", json.dumps({"error": "Invalid JSON"}).encode())
            return

        asset_id = payload.get("asset_id") if isinstance(payload, dict) else None
        tags = payload.get("tags") if isinstance(payload, dict) else None
        if (
            not isinstance(asset_id, str)
            or not isinstance(tags, list)
            or not all(isinstance(tag, str) for tag in tags)
        ):
            self._respond(400, "application/json", json.dumps({"error": "Missing asset_id or tags"}).encode())
            return

        with self.ctx.lock:
            error = self.ctx.set_selected_tags(asset_id, tags)
            selected = self.ctx.get_selected_tags(asset_id) if error is None else None

        if error:
            self._respond(400, "application/json", json.dumps({"error": error}).encode())
        else:
            body_out = json.dumps({"ok": True, "tags": selected}, ensure_ascii=False).encode("utf-8")
            self._respond(200, "application/json", body_out)

    def _handle_select_custom(self) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, "application/json", json.dumps({"error": "Invalid JSON"}).encode())
            return
        if not isinstance(payload, dict):
            self._respond(400, "application/json", json.dumps({"error": "Expected JSON object"}).encode())
            return

        asset_id = payload.get("asset_id")
        text = payload.get("text")
        based_on = payload.get("based_on_candidate_id")
        if not isinstance(asset_id, str) or not isinstance(text, str):
            self._respond(400, "application/json", json.dumps({"error": "Missing asset_id or text"}).encode())
            return
        if based_on is not None and not isinstance(based_on, str):
            self._respond(
                400, "application/json", json.dumps({"error": "based_on_candidate_id must be a string"}).encode()
            )
            return

        with self.ctx.lock:
            error = self.ctx.select_custom_text(asset_id, text, based_on)
            current = None if error else read_current_selection(self.ctx.store.layout, asset_id)

        if error:
            self._respond(400, "application/json", json.dumps({"error": error}).encode())
            return
        saved_text = current[1] if current is not None else text
        body_out = json.dumps({"ok": True, "text": saved_text}, ensure_ascii=False).encode("utf-8")
        self._respond(200, "application/json", body_out)

    def _handle_refine(self) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._respond_json(400, {"error": "Invalid JSON"})
            return
        if not isinstance(payload, dict):
            self._respond_json(400, {"error": "Expected JSON object"})
            return
        asset_id = payload.get("asset_id")
        instruction = payload.get("instruction")
        candidate_id = payload.get("candidate_id")
        text = payload.get("text")
        if not isinstance(asset_id, str) or not isinstance(instruction, str):
            self._respond_json(400, {"error": "Missing asset_id or instruction"})
            return
        if (candidate_id is not None and not isinstance(candidate_id, str)) or (
            text is not None and not isinstance(text, str)
        ):
            self._respond_json(400, {"error": "candidate_id and text must be strings"})
            return

        with self.ctx.lock:
            prepared = self.ctx.prepare_refine(asset_id, instruction, candidate_id, text)
            if isinstance(prepared, str):
                self._respond_json(400, {"error": prepared})
                return
            if asset_id in self.ctx.refining:
                self._respond_json(409, {"error": f"A refinement of {asset_id} is already running"})
                return
            self.ctx.refining.add(asset_id)
        try:
            # The drafter call can take minutes: run it without holding the lock.
            candidate = asset_refine_llm.refine_asset_text(workspace=self.ctx.store.layout.root, request=prepared)
            with self.ctx.lock:
                self.ctx.add_candidate(candidate)
        except Exception as exc:
            self._respond_json(502, {"error": f"Refinement failed: {exc}"})
            return
        finally:
            with self.ctx.lock:
                self.ctx.refining.discard(asset_id)
        self._respond_json(
            200,
            {
                "ok": True,
                "candidate": {
                    "candidate_id": str(candidate.candidate_id),
                    "content": candidate.content,
                    "content_html": markdown_to_deterministic_html(candidate.content),
                    "format": candidate.format.value,
                    "instruction": prepared.instruction,
                },
            },
        )

    def _respond_json(self, status: int, data: dict[str, object]) -> None:
        self._respond(status, "application/json", json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _handle_done(self) -> None:
        self._respond(200, "application/json", json.dumps({"ok": True}).encode())
        # Shut down from a background thread to avoid deadlock
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _read_body(self) -> bytes | None:
        length_str = self.headers.get("Content-Length")
        if length_str is None:
            self._respond(411, "text/plain", b"Content-Length required")
            return None
        try:
            length = int(length_str)
        except ValueError:
            self._respond(400, "text/plain", b"Invalid Content-Length")
            return None
        return self.rfile.read(length)

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _build_html_page() -> str:
    return """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Podcast Pipeline – Pick Candidates</title>
<style>
  :root {
    --bg: #f5f5f5; --card-bg: #fff; --border: #ddd; --accent: #2563eb;
    --accent-hover: #1d4ed8; --selected-bg: #e0f2fe; --selected-border: #2563eb;
    --text: #1a1a1a; --text-muted: #666; --success: #16a34a;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--text); }
  header { background: var(--card-bg); border-bottom: 1px solid var(--border);
           padding: 1rem 2rem; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 1.25rem; }
  .progress { color: var(--text-muted); font-size: 0.9rem; }
  .done-btn { background: var(--success); color: #fff; border: none; padding: 0.5rem 1.5rem;
              border-radius: 6px; cursor: pointer; font-size: 0.9rem; font-weight: 600; }
  .done-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .done-btn:not(:disabled):hover { filter: brightness(0.9); }
  .container { display: flex; height: calc(100vh - 60px); }
  .sidebar { width: 240px; min-width: 200px; border-right: 1px solid var(--border);
             background: var(--card-bg); overflow-y: auto; }
  .sidebar-item { padding: 0.75rem 1rem; cursor: pointer; border-bottom: 1px solid var(--border);
                  font-size: 0.85rem; display: flex; justify-content: space-between; align-items: center; }
  .sidebar-item:hover { background: #f0f0f0; }
  .sidebar-item.active { background: var(--selected-bg); font-weight: 600; }
  .sidebar-item .check { color: var(--success); font-weight: bold; }
  .main { flex: 1; overflow-y: auto; padding: 1.5rem; }
  .asset-header { font-size: 1.1rem; margin-bottom: 1rem; font-weight: 600; }
  .candidates-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
                     gap: 1rem; }
  .candidate-card { background: var(--card-bg); border: 2px solid var(--border);
                    border-radius: 8px; padding: 1rem; position: relative; }
  .candidate-card.selected { border-color: var(--selected-border); background: var(--selected-bg); }
  .candidate-card .card-header { display: flex; justify-content: space-between;
                                 align-items: center; margin-bottom: 0.75rem; }
  .candidate-card .card-label { font-size: 0.8rem; color: var(--text-muted); }
  .select-btn { background: var(--accent); color: #fff; border: none; padding: 0.35rem 1rem;
                border-radius: 4px; cursor: pointer; font-size: 0.8rem; }
  .select-btn:hover { background: var(--accent-hover); }
  .candidate-card.selected .select-btn { background: var(--success); }
  .candidate-content { font-size: 0.9rem; line-height: 1.6; max-height: 60vh; overflow-y: auto; }
  .candidate-content h1, .candidate-content h2, .candidate-content h3 { margin: 0.5em 0 0.25em; }
  .candidate-content p { margin: 0.4em 0; }
  .candidate-content ul, .candidate-content ol { padding-left: 1.5em; margin: 0.4em 0; }
  .tag-hint { color: var(--text-muted); font-size: 0.85rem; margin-bottom: 0.75rem; }
  .tag-toolbar { display: flex; gap: 0.5rem; align-items: center; margin-bottom: 0.75rem; }
  .tag-toolbar input { flex: 1; max-width: 360px; padding: 0.35rem 0.5rem; border: 1px solid var(--border);
                       border-radius: 4px; }
  .tag-btn { background: var(--card-bg); border: 1px solid var(--border); padding: 0.35rem 0.8rem;
             border-radius: 4px; cursor: pointer; font-size: 0.8rem; }
  .tag-pool { display: flex; flex-wrap: wrap; gap: 0.4rem; }
  .tag-chip { border: 1px solid var(--border); background: var(--card-bg); border-radius: 999px;
              padding: 0.25rem 0.7rem; font-size: 0.85rem; cursor: pointer; }
  .tag-chip.on { border-color: var(--selected-border); background: var(--selected-bg); font-weight: 600; }
  .tag-chip:disabled { opacity: 0.45; cursor: not-allowed; }
  .tag-chip .count { color: var(--text-muted); font-weight: 400; margin-left: 0.3rem; font-size: 0.75rem; }
  .card-actions { display: flex; gap: 0.4rem; }
  .edit-btn { background: transparent; color: var(--accent); border: 1px solid var(--accent);
              padding: 0.35rem 0.8rem; border-radius: 4px; cursor: pointer; font-size: 0.8rem; }
  .edit-btn:hover { background: var(--selected-bg); }
  .custom-card { margin-top: 1rem; }
  .custom-card textarea { width: 100%; min-height: 4.5rem; font-family: ui-monospace, SFMono-Regular, monospace;
                          font-size: 0.85rem; padding: 0.5rem; border: 1px solid var(--border); border-radius: 4px;
                          resize: vertical; }
  .custom-card textarea.multiline { min-height: 14rem; }
  .custom-hint { font-size: 0.8rem; color: var(--text-muted); margin: 0.25rem 0 0.5rem; }
  .custom-error { font-size: 0.8rem; color: #dc2626; margin-top: 0.5rem; white-space: pre-wrap; }
  .custom-error:empty { display: none; }
  .refine-row { display: flex; gap: 0.5rem; margin-top: 0.75rem; align-items: center; }
  .refine-row select, .refine-row input { font-size: 0.85rem; padding: 0.35rem 0.5rem;
                                         border: 1px solid var(--border); border-radius: 4px; }
  .refine-row input { flex: 1; }
  .select-btn:disabled { opacity: 0.5; cursor: wait; }
</style>
</head>
<body>
<header>
  <h1>Pick Candidates</h1>
  <span class="progress" id="progress">Loading...</span>
  <button class="done-btn" id="done-btn" disabled onclick="handleDone()">Done</button>
</header>
<div class="container">
  <nav class="sidebar" id="sidebar"></nav>
  <main class="main" id="main">
    <p style="color:var(--text-muted)">Select an asset from the sidebar.</p>
  </main>
</div>
<script>
let assets = [];
let activeAsset = null;
// Unsaved custom-text drafts per asset: {text, basedOn}
const drafts = {};
// Assets with a refine request in flight
const refining = new Set();
const SINGLE_LINE_ASSETS = new Set(["slug", "title_detail", "title_seo", "subtitle_auphonic"]);

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

async function init() {
  const resp = await fetch("/api/assets");
  assets = await resp.json();
  renderSidebar();
  updateProgress();
  if (assets.length > 0) selectAsset(assets[0].asset_id);
}

function renderSidebar() {
  const sb = document.getElementById("sidebar");
  sb.innerHTML = "";
  for (const a of assets) {
    const div = document.createElement("div");
    div.className = "sidebar-item" + (activeAsset === a.asset_id ? " active" : "");
    const label = document.createElement("span");
    label.textContent = a.asset_id;
    div.appendChild(label);
    if (a.is_tag_asset) {
      const n = document.createElement("span");
      n.className = "check";
      n.textContent = `${(a.selected_tags || []).length}/${a.max_tags}`;
      div.appendChild(n);
    } else if (a.has_selection) {
      const chk = document.createElement("span");
      chk.className = "check";
      chk.innerHTML = "&#10003;";
      div.appendChild(chk);
    }
    div.onclick = () => selectAsset(a.asset_id);
    sb.appendChild(div);
  }
}

function selectAsset(assetId) {
  activeAsset = assetId;
  renderSidebar();
  renderMain();
}

function escAttr(s) {
  return esc(s).replace(/"/g, "&quot;");
}

function tagKey(tag) {
  return String(tag).trim().toLowerCase();
}

function renderTagMain(main, asset) {
  const aid = esc(asset.asset_id);
  const selected = asset.selected_tags || [];
  const selectedKeys = new Set(selected.map(tagKey));
  const atLimit = selected.length >= asset.max_tags;
  const poolKeys = new Set(asset.tag_pool.map(e => tagKey(e.tag)));
  let html = `<div class="asset-header">${aid} (${selected.length}/${asset.max_tags})</div>`;
  html += `<p class="tag-hint">Suggestions from ${asset.candidates.length} candidate(s), most proposed first.
    Click to toggle; at most ${asset.max_tags} entries.</p>`;
  html += `<div class="tag-toolbar">
    <input id="tag-input" placeholder="Add tag (comma-separated)">
    <button class="tag-btn" id="tag-add">Add</button>
    <button class="tag-btn" id="tag-top">Use top ${Math.min(asset.max_tags, asset.tag_pool.length)}</button>
    <button class="tag-btn" id="tag-clear">Clear</button>
  </div>`;
  html += '<div class="tag-pool">';
  const extras = selected.filter(t => !poolKeys.has(tagKey(t)));
  for (const t of extras) {
    html += `<button class="tag-chip on" data-tag="${escAttr(t)}">${esc(t)}</button>`;
  }
  for (const e of asset.tag_pool) {
    const on = selectedKeys.has(tagKey(e.tag));
    const disabled = !on && atLimit ? " disabled" : "";
    html += `<button class="tag-chip${on ? " on" : ""}" data-tag="${escAttr(e.tag)}"${disabled}>`
      + `${esc(e.tag)}<span class="count">&times;${e.count}</span></button>`;
  }
  html += "</div>";
  main.innerHTML = html;
  main.querySelectorAll(".tag-chip").forEach(btn => {
    btn.onclick = () => {
      const tag = btn.dataset.tag;
      const key = tagKey(tag);
      updateTags(asset, current => current.some(t => tagKey(t) === key)
        ? current.filter(t => tagKey(t) !== key)
        : [...current, tag]);
    };
  });
  const input = document.getElementById("tag-input");
  const add = () => {
    const extra = input.value.split(",").map(t => t.trim()).filter(Boolean);
    if (extra.length) updateTags(asset, current => [...current, ...extra]);
  };
  document.getElementById("tag-add").onclick = add;
  input.onkeydown = (ev) => { if (ev.key === "Enter") add(); };
  document.getElementById("tag-top").onclick = () =>
    updateTags(asset, () => asset.tag_pool.slice(0, asset.max_tags).map(e => e.tag));
  document.getElementById("tag-clear").onclick = () => updateTags(asset, () => []);
}

// Tag saves run one at a time and each computes its selection from the state the
// previous save left, so quick successive clicks never overwrite each other.
let tagQueue = Promise.resolve();

function updateTags(asset, compute) {
  tagQueue = tagQueue
    .then(() => saveTags(asset, compute(asset.selected_tags || [])))
    .catch(err => alert(err instanceof Error ? err.message : String(err)));
  return tagQueue;
}

async function saveTags(asset, tags) {
  const resp = await fetch("/api/tags", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({asset_id: asset.asset_id, tags: tags})
  });
  const data = await resp.json();
  if (!resp.ok) {
    alert(data.error);
    return;
  }
  asset.selected_tags = data.tags;
  asset.selected_candidate_id = null;
  renderSidebar();
  renderMain();
  updateProgress();
}

function renderMain() {
  const main = document.getElementById("main");
  const asset = assets.find(a => a.asset_id === activeAsset);
  if (!asset) { main.innerHTML = ""; return; }
  if (asset.is_tag_asset) { renderTagMain(main, asset); return; }
  const aid = esc(asset.asset_id);
  let html = `<div class="asset-header">${aid}</div>`;
  html += '<div class="candidates-grid">';
  for (let i = 0; i < asset.candidates.length; i++) {
    const c = asset.candidates[i];
    const isSel = c.candidate_id === asset.selected_candidate_id;
    const cls = isSel ? "candidate-card selected" : "candidate-card";
    const cid = esc(c.candidate_id);
    html += `<div class="${cls}" id="card-${cid}">
      <div class="card-header">
        <span class="card-label">Candidate ${i + 1} (${esc(c.format)})${
          c.instruction ? ` · refined: ${esc(c.instruction)}` : ""}</span>
        <span class="card-actions">
          <button class="edit-btn" data-candidate="${cid}"
            title="Copy this candidate into the custom text editor">Edit</button>
          <button class="select-btn"
            data-asset="${aid}" data-candidate="${cid}">
            ${isSel ? "Selected" : "Select"}
          </button>
        </span>
      </div>
      <div class="candidate-content">${c.content_html}</div>
    </div>`;
  }
  html += "</div>";
  html += renderCustomCard(asset);
  main.innerHTML = html;
  main.querySelectorAll(".select-btn").forEach(btn => {
    btn.onclick = () => handleSelect(
      btn.dataset.asset, btn.dataset.candidate
    );
  });
  main.querySelectorAll(".edit-btn").forEach(btn => {
    btn.onclick = () => startFromCandidate(asset, btn.dataset.candidate);
  });
  const textarea = document.getElementById("custom-text");
  textarea.oninput = () => {
    currentDraft(asset).text = textarea.value;
  };
  document.getElementById("custom-save").onclick = () => handleSaveCustom(asset.asset_id);
  document.getElementById("custom-clear").onclick = () => {
    drafts[asset.asset_id] = {...currentDraft(asset), text: "", basedOn: null};
    renderMain();
  };
  const instruction = document.getElementById("refine-instruction");
  instruction.oninput = () => {
    currentDraft(asset).instruction = instruction.value;
  };
  instruction.onkeydown = (event) => {
    if (event.key === "Enter") handleRefine(asset.asset_id);
  };
  document.getElementById("refine-source").onchange = (event) => {
    currentDraft(asset).refineSource = event.target.value;
  };
  document.getElementById("refine-btn").onclick = () => handleRefine(asset.asset_id);
}

function refineSourceOptions(asset, draft) {
  // Default source: the editor if it has text, else the selected candidate, else the first one.
  const fallback = draft.text.trim() ? "editor" : (asset.selected_candidate_id || asset.candidates[0]?.candidate_id);
  const chosen = draft.refineSource || fallback;
  let html = `<option value="editor"${chosen === "editor" ? " selected" : ""}>Custom text (editor)</option>`;
  asset.candidates.forEach((c, i) => {
    const sel = c.candidate_id === chosen ? " selected" : "";
    html += `<option value="${esc(c.candidate_id)}"${sel}>Candidate ${i + 1}</option>`;
  });
  return html;
}

async function handleRefine(assetId) {
  const asset = assets.find(a => a.asset_id === assetId);
  if (!asset || refining.has(assetId)) return;
  const draft = currentDraft(asset);
  draft.text = document.getElementById("custom-text").value;
  draft.instruction = document.getElementById("refine-instruction").value;
  draft.refineSource = document.getElementById("refine-source").value;
  const body = {asset_id: assetId, instruction: draft.instruction};
  if (draft.refineSource === "editor") body.text = draft.text;
  else body.candidate_id = draft.refineSource;

  refining.add(assetId);
  renderMain();
  let data;
  let ok = false;
  try {
    const resp = await fetch("/api/refine", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)
    });
    data = await resp.json();
    ok = resp.ok;
  } catch (err) {
    data = {error: String(err)};
  } finally {
    refining.delete(assetId);
  }
  if (ok) {
    asset.candidates.push(data.candidate);
    draft.refineSource = data.candidate.candidate_id;
    draft.refineStatus = `Added Candidate ${asset.candidates.length}. Select it, press "Edit", or refine it further.`;
  } else {
    draft.refineStatus = "";
    draft.error = data.error;
  }
  if (activeAsset === assetId) renderMain();
}

function currentDraft(asset) {
  if (!drafts[asset.asset_id]) {
    drafts[asset.asset_id] = {text: asset.custom_selected ? (asset.selected_text || "").trimEnd() : "", basedOn: null};
  }
  return drafts[asset.asset_id];
}

function editableText(asset, candidate) {
  if (asset.selection_format !== "plain") return candidate.content.trimEnd();
  // Plain selections carry only the value: drop markdown headings and blank lines.
  const lines = candidate.content.split("\\n").map(l => l.trim()).filter(l => l && !l.startsWith("#"));
  return lines.join(SINGLE_LINE_ASSETS.has(asset.asset_id) ? " " : "\\n");
}

function startFromCandidate(asset, candidateId) {
  const c = asset.candidates.find(item => item.candidate_id === candidateId);
  if (!c) return;
  drafts[asset.asset_id] = {...currentDraft(asset), text: editableText(asset, c), basedOn: candidateId};
  renderMain();
  const textarea = document.getElementById("custom-text");
  textarea.focus();
  textarea.scrollIntoView({block: "center"});
}

function renderCustomCard(asset) {
  const draft = currentDraft(asset);
  const cls = asset.custom_selected ? "candidate-card custom-card selected" : "candidate-card custom-card";
  const multiline = SINGLE_LINE_ASSETS.has(asset.asset_id) ? "" : " multiline";
  let hint = `Write your own ${esc(asset.asset_id)} or press "Edit" on a candidate to start from it. ` +
    `Saved as ${esc(asset.selection_format)} text.`;
  if (asset.asset_id === "slug") hint += " Slugs use lowercase letters, digits and hyphens (e.g. python-3-15).";
  const busy = refining.has(asset.asset_id);
  const status = busy ? "Refining with the drafter agent… this can take a minute." : (draft.refineStatus || "");
  const error = draft.error || "";
  draft.error = "";
  return `<div class="${cls}">
    <div class="card-header">
      <span class="card-label">Custom text${asset.custom_selected ? " (current selection)" : ""}</span>
      <span class="card-actions">
        <button class="edit-btn" id="custom-clear">Clear</button>
        <button class="select-btn" id="custom-save">${asset.custom_selected ? "Save" : "Use custom text"}</button>
      </span>
    </div>
    <p class="custom-hint">${hint}</p>
    <textarea id="custom-text" class="${multiline.trim()}" spellcheck="true">${esc(draft.text)}</textarea>
    <div class="refine-row">
      <select id="refine-source" title="Text to refine">${refineSourceOptions(asset, draft)}</select>
      <input id="refine-instruction" type="text" value="${esc(draft.instruction || "").replace(/"/g, "&quot;")}"
        placeholder="Instruction for the drafter, e.g. kürzer, ohne Emojis, Slug nur python-315">
      <button class="select-btn" id="refine-btn"${busy ? " disabled" : ""}>Refine</button>
    </div>
    <p class="custom-hint" id="refine-status">${esc(status)}</p>
    <div class="custom-error" id="custom-error">${esc(error)}</div>
  </div>`;
}

async function handleSaveCustom(assetId) {
  const asset = assets.find(a => a.asset_id === assetId);
  if (!asset) return;
  const draft = currentDraft(asset);
  draft.text = document.getElementById("custom-text").value;
  const resp = await fetch("/api/select-custom", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({asset_id: assetId, text: draft.text, based_on_candidate_id: draft.basedOn})
  });
  const data = await resp.json();
  if (!resp.ok) {
    document.getElementById("custom-error").textContent = data.error;
    return;
  }
  asset.selected_candidate_id = null;
  asset.custom_selected = true;
  asset.has_selection = true;
  asset.selected_text = data.text;
  drafts[assetId] = {...draft, text: data.text.trimEnd(), basedOn: null};
  renderSidebar();
  renderMain();
  updateProgress();
}

async function handleSelect(assetId, candidateId) {
  const resp = await fetch("/api/select", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({asset_id: assetId, candidate_id: candidateId})
  });
  if (!resp.ok) {
    const err = await resp.json();
    alert(err.error);
    return;
  }
  const asset = assets.find(a => a.asset_id === assetId);
  if (asset) {
    asset.selected_candidate_id = candidateId;
    asset.custom_selected = false;
    asset.has_selection = true;
  }
  renderSidebar();
  renderMain();
  updateProgress();
}

function updateProgress() {
  // Tag assets are curated by toggling tags and never block Done.
  const required = assets.filter(a => !a.is_tag_asset);
  const done = required.filter(a => a.has_selection).length;
  document.getElementById("progress").textContent = `${done}/${required.length} selected`;
  document.getElementById("done-btn").disabled = done < required.length;
}

async function handleDone() {
  // Let queued tag saves finish before the server shuts down.
  await tagQueue;
  await fetch("/api/done", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
  const s = "display:flex;align-items:center;justify-content:center;";
  document.body.innerHTML = `<div style='${s}height:100vh;font-size:1.5rem;color:#16a34a'>All done!</div>`;
}

init();
</script>
</body>
</html>"""
