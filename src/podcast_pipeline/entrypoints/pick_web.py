from __future__ import annotations

import json
import sys
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import UUID

from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace
from podcast_pipeline.markdown_html import markdown_to_deterministic_html
from podcast_pipeline.pick_core import (
    build_asset,
    find_candidate_by_id,
    load_candidates,
    load_workspace,
    update_workspace_assets,
    validate_asset_id,
)
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
    server = HTTPServer(("127.0.0.1", 0), handler)
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
        self.lock = threading.Lock()

    def get_assets_json(self) -> list[dict[str, object]]:
        ws = self.workspace_state
        assets_by_id = {asset.asset_id: asset for asset in ws.assets}

        result: list[dict[str, object]] = []
        for asset_key in sorted(self.candidates_by_asset):
            candidates = self.candidates_by_asset[asset_key]
            existing = assets_by_id.get(asset_key)
            selected_id = str(existing.selected_candidate_id) if existing and existing.selected_candidate_id else None

            candidate_items: list[dict[str, object]] = []
            for c in candidates:
                candidate_items.append(
                    {
                        "candidate_id": str(c.candidate_id),
                        "content": c.content,
                        "content_html": markdown_to_deterministic_html(c.content),
                        "format": c.format.value,
                    }
                )

            result.append(
                {
                    "asset_id": asset_key,
                    "selected_candidate_id": selected_id,
                    "candidates": candidate_items,
                }
            )
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

        self.store.write_selected_text(asset_id, match.format, match.content)
        assets_by_id[asset_id] = build_asset(
            asset_id=asset_id,
            existing=existing,
            candidates=candidates,
            selected_candidate_id=match.candidate_id,
        )
        self.workspace_state = update_workspace_assets(ws, assets_by_id)
        self.store.write_state(self.workspace_state)
        return None


class _PickWebHandler(BaseHTTPRequestHandler):
    def __init__(self, ctx: _ServerContext, *args: object, **kwargs: object) -> None:
        self.ctx = ctx
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Suppress default stderr logging
        pass

    def do_GET(self) -> None:
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/assets":
            self._serve_assets_json()
        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self) -> None:
        if self.path == "/api/select":
            self._handle_select()
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
    if (a.selected_candidate_id) {
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

function renderMain() {
  const main = document.getElementById("main");
  const asset = assets.find(a => a.asset_id === activeAsset);
  if (!asset) { main.innerHTML = ""; return; }
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
        <span class="card-label">Candidate ${i + 1} (${esc(c.format)})</span>
        <button class="select-btn"
          data-asset="${aid}" data-candidate="${cid}">
          ${isSel ? "Selected" : "Select"}
        </button>
      </div>
      <div class="candidate-content">${c.content_html}</div>
    </div>`;
  }
  html += "</div>";
  main.innerHTML = html;
  main.querySelectorAll(".select-btn").forEach(btn => {
    btn.onclick = () => handleSelect(
      btn.dataset.asset, btn.dataset.candidate
    );
  });
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
  if (asset) asset.selected_candidate_id = candidateId;
  renderSidebar();
  renderMain();
  updateProgress();
}

function updateProgress() {
  const total = assets.length;
  const done = assets.filter(a => a.selected_candidate_id).length;
  document.getElementById("progress").textContent = `${done}/${total} selected`;
  document.getElementById("done-btn").disabled = done < total;
}

async function handleDone() {
  await fetch("/api/done", {method: "POST"});
  const s = "display:flex;align-items:center;justify-content:center;";
  document.body.innerHTML = `<div style='${s}height:100vh;font-size:1.5rem;color:#16a34a'>All done!</div>`;
}

init();
</script>
</body>
</html>"""
