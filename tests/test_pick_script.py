"""Pure exchange/draft regressions; DOM and keyboard coverage lives in Chromium tests."""

import json
import shutil
import subprocess

import pytest

from podcast_pipeline.entrypoints.pick_web import _build_html_page

SETUP = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
global.document = {getElementById: id => fields[id]};
const fields = {'custom-text':{value:'A'}, 'custom-error':{textContent:''},
 'refine-instruction':{value:'shorter'},'refine-source':{value:'editor'}};
const realTimeout = setTimeout;
global.setTimeout = (fn, delay) => realTimeout(fn, delay === 30000 ? 10 : delay);
global.alert = () => {};
"""
INIT = r"""
renderMain = renderSidebar = updateProgress = () => {};
assets = ['description', 'shownotes'].map(asset_id => ({asset_id, candidates:[], has_selection:false}));
activeAsset = 'description';
const response = data => ({ok:true, json: async () => data});
"""


def run_script(code: str, html: str | None = None) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node exchange tests require node; Chromium remains the UI gate")
    script = (html or _build_html_page()).split("<script>")[1].split("</script>")[0].rsplit("init();", 1)[0]
    result = subprocess.run(
        [node],
        input=SETUP
        + "\nvm.runInThisContext("
        + json.dumps(script)
        + ");\n"
        + INIT
        + "\nconst watchdog = realTimeout(() => {console.error('Unsettled operation'); process.exit(1)}, 1000);"
        + "\n(async () => {"
        + code
        + "})().then(() => clearTimeout(watchdog)).catch(e => {"
        + "clearTimeout(watchdog);console.error(e);process.exitCode=1});",
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("body_stall", [False, True])
@pytest.mark.parametrize("operation", ["select", "custom", "refine", "tags", "assets", "done"])
def test_exchange_deadline_settles_and_releases_choice(body_stall: bool, operation: str) -> None:
    stall = "{ok:true,json:() => new Promise(() => {})}" if body_stall else "new Promise(() => {})"
    actions = {
        "select": "handleSelect('description','c1')",
        "custom": "handleSaveCustom('description')",
        "refine": "handleRefine('description')",
        "tags": "saveTags(assets[0], ['Python'])",
        "assets": "init()",
        "done": "handleDone()",
    }
    code = r"""
      fetch = async path => path === '/api/assets' && 'OPERATION' !== 'assets' ? response(assets) : STALL;
      let settled = false;
      Promise.resolve(ACTION).catch(() => {}).finally(() => settled = true);
      await new Promise(resolve => realTimeout(resolve, 80));
      assert.equal(settled, true);
      assert.equal(pendingChoices.size, 0);
      assert.equal(refining.size, 0);
      fetch = async path => path === '/api/assets' ? response(assets) : response({ok:true});
      await handleSelect('description','c2');
      assert.equal(assets[0].selected_candidate_id, 'c2');
    """
    run_script(code.replace("STALL", stall).replace("ACTION", actions[operation]).replace("OPERATION", operation))


def test_selection_one_pending_choice() -> None:
    run_script(r"""
      let finish;
      let calls = 0;
      fetch = async () => { calls++; return new Promise(resolve => finish = resolve); };
      const first = handleSelect('description','c1');
      await handleSelect('description','c2');
      assert.equal(calls, 1);
      finish(response({ok:true}));
      await first;
      assert.equal(assets[0].selected_candidate_id, 'c1');
    """)


def test_custom_save_acknowledges_only_submitted_revision() -> None:
    run_script(r"""
      let finish;
      fetch = async () => new Promise(resolve => finish = resolve);
      const saving = handleSaveCustom('description');
      currentDraft(assets[0]).text = 'B';
      currentDraft(assets[0]).revision = 1;
      currentDraft(assets[0]).dirty = true;
      activeAsset = 'shownotes';
      fields['custom-text'].value = 'Other editor';
      finish(response({text:'A'}));
      await saving;
      assert.equal(drafts.description.text, 'B');
      assert.equal(drafts.description.dirty, true);
      assert.equal(fields['custom-text'].value, 'Other editor');
    """)


@pytest.mark.parametrize(
    "event",
    [
        "{key:'Enter',isComposing:true}",
        "{key:'Enter',keyCode:229}",
        "{key:'Enter',defaultPrevented:true}",
    ],
)
def test_composing_enter_is_not_submission(event: str) -> None:
    run_script(f"assert.equal(committedEnter({event}), false); assert.equal(committedEnter({{key:'Enter'}}), true);")


def test_focus_restores_equivalent_control_and_caret() -> None:
    run_script(r"""
      let focused = false, caret;
      const old = {id:'custom-text',isConnected:true,selectionStart:2,selectionEnd:5,selectionDirection:'forward'};
      document.activeElement = old;
      document.querySelector = () => ({focus:() => focused = true, setSelectionRange:(...args) => caret = args});
      global.CSS = {escape: value => value};
      withFocus(() => old.isConnected = false);
      assert.equal(focused, true);
      assert.deepEqual(caret, [2,5,'forward']);
    """)


def test_sidebar_uses_native_buttons() -> None:
    run_script(r"""
      const nodes = [];
      const sidebar = {appendChild: node => nodes.push(node)};
      document.getElementById = () => sidebar;
      document.createElement = tag => ({tag, dataset:{}, appendChild:() => {}, setAttribute:() => {}});
      _renderSidebar();
      assert.equal(nodes.length, 2);
      assert.equal(nodes[0].tag, 'button');
      assert.equal(nodes[0].type, 'button');
      assert.equal(nodes[0].dataset.assetId, 'description');
    """)


def test_custom_error_stays_with_submitting_asset() -> None:
    run_script(r"""
      let finish;
      fetch = async path => path === '/api/assets' ? response(assets) : new Promise(resolve => finish = resolve);
      const saving = handleSaveCustom('description');
      activeAsset = 'shownotes';
      fields['custom-error'].textContent = '';
      finish({ok:false,json:async () => ({error:'Save failed'})});
      await saving;
      assert.equal(fields['custom-error'].textContent, '');
      assert.match(drafts.description.error, /Save failed/);
    """)


def test_matching_custom_ack_clears_its_dirty_state() -> None:
    run_script(r"""
      currentDraft(assets[0]).dirty = true;
      fetch = async () => response({text:'A'});
      await handleSaveCustom('description');
      assert.equal(drafts.description.dirty, false);
      assert.equal(drafts.description.text, 'A');
    """)
