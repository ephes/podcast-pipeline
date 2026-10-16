"""Run the picking pages' JavaScript in Node to check queued tag saves.

A minimal fake DOM and a slow fake ``fetch`` reproduce quick successive tag toggles
followed by Done/Close: every toggle must be saved, in order, before shutdown.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from podcast_pipeline.entrypoints.pick_web import _build_html_page

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="node is not installed")

_DASHBOARD_HTML = Path(__file__).resolve().parents[1] / "src" / "podcast_pipeline" / "static" / "dashboard.html"

_FAKE_ENV = r"""
process.on("unhandledRejection", () => {});
function makeStub(name) {
  const store = {};
  const fn = function () { return makeStub(name + "()"); };
  return new Proxy(fn, {
    get(_target, prop) {
      if (prop === "then") return undefined;
      if (prop === Symbol.toPrimitive) return () => "";
      if (prop in store) return store[prop];
      if (prop === "innerHTML") return store.textContent === undefined ? "" : String(store.textContent);
      if (prop === "querySelectorAll") return () => [];
      if (prop === "closest") return () => null;
      if (prop === "value") return "";
      const child = makeStub(name + "." + String(prop));
      store[prop] = child;
      return child;
    },
    set(_target, prop, value) { store[prop] = value; return true; },
  });
}
globalThis.document = makeStub("document");
globalThis.window = globalThis;
globalThis.EventSource = function () { return makeStub("eventsource"); };
globalThis.alert = () => {};
globalThis.confirm = () => true;
const mutations = [];
let serverTags = [];
function respond(body) {
  const text = JSON.stringify(body);
  return { ok: true, status: 200, json: async () => body, text: async () => text };
}
globalThis.fetch = async (path, options = {}) => {
  const method = options.method || "GET";
  if (method === "GET") {
    if (path === "/api/assets") return respond(ASSETS());
    if (path === "/api/status") return respond({ workspace: "", episode_id: "", hosts: [], stages: {} });
    if (path === "/api/jobs") return respond([]);
    return respond({});
  }
  const body = options.body ? JSON.parse(options.body) : {};
  mutations.push([method, path, body.tags === undefined ? null : body.tags]);
  // Slow saves so the next click arrives while the previous save is still pending.
  await new Promise((resolve) => setTimeout(resolve, 30));
  if (body.tags !== undefined) serverTags = body.tags;
  return respond(body.tags === undefined ? { ok: true } : { ok: true, tags: serverTags });
};
function ASSETS() {
  return [{
    asset_id: "itunes_keywords",
    selected_candidate_id: null,
    has_selection: serverTags.length > 0,
    candidates: [],
    is_tag_asset: true,
    selected_tags: serverTags,
    tag_pool: [{ tag: "pypy", count: 2 }, { tag: "jit", count: 1 }],
    max_tags: 12,
  }];
}
"""


def _run(script: str) -> list[list[object]]:
    assert _NODE is not None
    result = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    mutations: list[list[object]] = json.loads(result.stdout.strip().splitlines()[-1])
    return mutations


def test_pick_web_done_waits_for_queued_tag_saves() -> None:
    page_script = re.search(r"<script>(.*)</script>", _build_html_page(), re.S)
    assert page_script is not None
    scenario = r"""
(async () => {
  await new Promise((resolve) => setTimeout(resolve, 50));  // let init() load assets
  const asset = assets.find((a) => a.asset_id === "itunes_keywords");
  updateTags(asset, (current) => [...current, "pypy"]);
  updateTags(asset, (current) => [...current, "jit"]);
  await handleDone();
  console.log(JSON.stringify(mutations));
})();
"""
    mutations = _run(_FAKE_ENV + page_script.group(1) + scenario)
    assert mutations == [
        ["POST", "/api/tags", ["pypy"]],
        ["POST", "/api/tags", ["pypy", "jit"]],
        ["POST", "/api/done", None],
    ]


def test_dashboard_close_waits_for_queued_tag_saves() -> None:
    page_script = "\n".join(re.findall(r"<script>(.*?)</script>", _DASHBOARD_HTML.read_text(encoding="utf-8"), re.S))
    scenario = r"""
(async () => {
  await new Promise((resolve) => setTimeout(resolve, 50));
  state.assets = ASSETS();
  addTags("itunes_keywords", ["pypy"]);
  removeTag("itunes_keywords", "pypy");
  addTags("itunes_keywords", ["jit"]);
  await handleDone();
  console.log(JSON.stringify(mutations));
})();
"""
    mutations = _run(_FAKE_ENV + page_script + scenario)
    assert mutations == [
        ["PUT", "/api/assets/itunes_keywords/tags", ["pypy"]],
        ["PUT", "/api/assets/itunes_keywords/tags", []],
        ["PUT", "/api/assets/itunes_keywords/tags", ["jit"]],
        ["POST", "/api/done", None],
    ]


def test_dashboard_double_click_on_suggestion_toggles_back() -> None:
    page_script = "\n".join(re.findall(r"<script>(.*?)</script>", _DASHBOARD_HTML.read_text(encoding="utf-8"), re.S))
    scenario = r"""
(async () => {
  await new Promise((resolve) => setTimeout(resolve, 50));
  state.assets = ASSETS();
  toggleTag("itunes_keywords", "pypy");
  await toggleTag("itunes_keywords", "pypy");
  console.log(JSON.stringify(mutations));
  process.exit(0);  // the dashboard's job polling timer would keep Node alive
})();
"""
    mutations = _run(_FAKE_ENV + page_script + scenario)
    assert mutations == [
        ["PUT", "/api/assets/itunes_keywords/tags", ["pypy"]],
        ["PUT", "/api/assets/itunes_keywords/tags", []],
    ]
