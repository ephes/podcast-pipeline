"""Synthetic browser regressions against the shipped dashboard, without a server or agents."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright

DASHBOARD = Path(__file__).parents[1] / "src/podcast_pipeline/static/dashboard.html"
FAKE_API = r"""
window.server = {
  assets: ['title_detail', 'description', 'cms_tags'].map(asset_id => ({
    asset_id, candidates: ['c1', 'c2'].map(candidate_id => ({
      candidate_id, format: 'plain', content: 'Synthetic copy', content_html: '<p>Synthetic copy</p>'
    })), selected_tags: [], tag_pool: [{tag: 'Python', count: 2}], selected_text: ''
  })),
  requests: [], pending: [], holds: [], fail: [], notes: {},
};
window.response = body => ({ok: true, text: async () => JSON.stringify(body)});
window.fetch = async (path, options = {}) => {
  const method = options.method || 'GET';
  const body = options.body ? JSON.parse(options.body) : {};
  const key = method + ' ' + path;
  server.requests.push({key, body});
  if (server.holds.includes(key)) {
    return new Promise((resolve, reject) => server.pending.push({key, resolve, reject, body}));
  }
  if (server.fail.includes(key)) throw new Error('Synthetic failure');
  if (path === '/api/assets') return response(server.assets);
  if (path === '/api/status') return response({stages: {}});
  if (path === '/api/jobs') return response([]);
  if (path === '/api/episode') return response({episode_id: 'synthetic', hosts: ['Host']});
  if (path.endsWith('/notes')) {
    if (method === 'PUT') server.notes[path] = body.notes;
    return response({notes: server.notes[path] || 'Stored notes'});
  }
  if (path.endsWith('/tags') && method === 'PUT') {
    server.assets.find(a => path.includes(a.asset_id)).selected_tags = body.tags;
  }
  if (method === 'DELETE') {
    const asset = server.assets.find(a => path.includes(a.asset_id));
    asset.candidates = asset.candidates.filter(c => !path.endsWith(c.candidate_id));
    server.assets = server.assets.filter(a => a.candidates.length);
  }
  return response({});
};
window.EventSource = class { addEventListener() {} close() {} };
window.alert = message => { window.lastAlert = message; };
window.confirm = () => true;
"""


@pytest.fixture
def page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.add_init_script(FAKE_API)
        page.route(
            "http://dashboard.test/", lambda route: route.fulfill(body=DASHBOARD.read_text(), content_type="text/html")
        )
        page.goto("http://dashboard.test/")
        page.wait_for_function("state.episodeData !== null")
        yield page
        browser.close()


def pick(page: Page, asset: str = "title_detail") -> None:
    page.locator('[data-tab="pick"]').click()
    page.locator(f'#pick-sidebar [data-asset-id="{asset}"]').click()
    expect(page.locator("#pick-notes")).to_have_attribute("data-asset-id", asset)


def test_render_generation(page: Page) -> None:
    pick(page)
    page.evaluate("server.holds = ['GET /api/assets/description/notes', 'GET /api/assets/title_detail/notes']")
    for asset in ["description", "title_detail", "description"]:
        page.locator(f'#pick-sidebar [data-asset-id="{asset}"]').click()
    page.wait_for_function("server.pending.length === 3")
    page.evaluate("server.pending[2].resolve(response({notes:'Winning notes'}))")
    expect(page.locator("#pick-notes")).to_have_value("Winning notes")
    page.evaluate(
        "server.pending[1].resolve(response({notes:'Stale A'}));server.pending[0].resolve(response({notes:'Stale B'}))"
    )
    page.wait_for_timeout(30)
    expect(page.locator("#pick-notes")).to_have_count(1)
    expect(page.locator("#pick-notes")).to_have_value("Winning notes")
    page.locator("#pick-custom-text").fill("Current text")
    page.locator('[data-action="save-custom"]').click()
    page.wait_for_function("server.requests.some(r => r.key === 'PUT /api/assets/description/selection')")
    assert page.evaluate("server.requests.find(r => r.key.endsWith('/selection')).body.text") == "Current text"


def test_notes_draft_retention(page: Page) -> None:
    pick(page)
    page.locator("#pick-notes").fill("Unsaved notes")
    page.locator('#pick-sidebar [data-asset-id="description"]').click()
    page.locator('#pick-sidebar [data-asset-id="title_detail"]').click()
    expect(page.locator("#pick-notes")).to_have_value("Unsaved notes")
    page.evaluate("server.fail = ['PUT /api/assets/title_detail/notes']")
    page.locator('[data-action="save-notes"]').click()
    page.wait_for_function("window.lastAlert?.includes('Failed')")
    page.evaluate("loadAssets()")
    expect(page.locator("#pick-notes")).to_have_value("Unsaved notes")
    page.evaluate("server.fail = []; server.holds = ['PUT /api/assets/title_detail/notes']")
    page.locator('[data-action="save-notes"]').click()
    page.wait_for_function("server.pending.length === 1")
    expect(page.locator('[data-action="save-notes"]')).to_be_disabled()
    count = page.evaluate("server.requests.length")
    page.evaluate("void saveNotes('title_detail')")
    assert page.evaluate("server.requests.length") == count
    page.locator("#pick-notes").fill("Newer notes")
    page.evaluate("server.pending[0].resolve(response({}))")
    page.wait_for_function("window.lastAlert === 'Notes saved.'")
    page.evaluate("loadAssets()")
    expect(page.locator("#pick-notes")).to_have_value("Newer notes")
    page.evaluate("server.holds = []")
    page.locator('[data-action="save-notes"]').click()
    page.wait_for_function("!Object.hasOwn(noteDrafts, 'title_detail')")


def test_custom_save_owns_only_submitted_draft(page: Page) -> None:
    pick(page)
    page.evaluate("server.holds = ['PUT /api/assets/title_detail/selection']")
    page.locator("#pick-custom-text").fill("A")
    page.locator('[data-action="save-custom"]').click()
    page.wait_for_function("server.pending.length === 1")
    expect(page.locator('[data-action="save-custom"]')).to_be_disabled()
    page.locator("#pick-custom-text").fill("B")
    page.evaluate("void saveCustomSelection('title_detail')")  # duplicate is refused while A is pending
    assert page.evaluate("server.pending.length") == 1
    page.evaluate("server.pending[0].resolve(response({}))")
    page.wait_for_function("!customSaves.has('title_detail')")
    expect(page.locator("#pick-custom-text")).to_have_value("B")
    expect(page.locator("#pick-custom-status")).to_have_text("Unsaved custom text.")
    page.evaluate("server.holds = []; server.fail = ['PUT /api/assets/title_detail/selection']")
    page.locator('[data-action="save-custom"]').click()
    expect(page.locator("#pick-custom-error")).to_have_text("Synthetic failure")
    page.evaluate("loadAssets()")
    expect(page.locator("#pick-custom-text")).to_have_value("B")
    expect(page.locator("#pick-custom-status")).to_have_text("Unsaved custom text.")
    page.evaluate("server.fail = []")
    page.locator('[data-action="save-custom"]').click()
    page.wait_for_function("!customSaves.has('title_detail') && customDrafts.title_detail.revision === 0")


def test_notes_save_preserves_streaming_job(page: Page) -> None:
    pick(page)
    page.evaluate("""() => {
      const log = document.getElementById('pick-regen-log');
      document.getElementById('pick-regen').style.display = '';
      window.originalLog = log;
      streamJob('synthetic', log, document.getElementById('pick-regen-progress'));
      state.currentJobStreams.synthetic.onmessage({data: JSON.stringify({type:'progress', message:'Before save'})});
    }""")
    page.locator("#pick-notes").fill("Updated")
    page.locator('[data-action="save-notes"]').click()
    page.wait_for_function("window.lastAlert === 'Notes saved.'")
    page.evaluate("""state.currentJobStreams.synthetic.onmessage({
      data: JSON.stringify({type:'progress', message:'After save'})
    })""")
    expect(page.locator("#pick-regen-log")).to_contain_text("After save")
    assert page.evaluate("originalLog.isConnected")
    expect(page.locator("#pick-regen")).to_be_visible()


def test_notes_ack_keeps_pending_candidate_refresh(page: Page) -> None:
    pick(page)
    page.locator("#pick-notes").fill("Saved during refresh")
    page.evaluate("""() => {
      server.assets[0].candidates.push({...server.assets[0].candidates[0], candidate_id:'c3'});
      server.holds = ['GET /api/assets/title_detail/notes'];
      void loadAssets();
    }""")
    page.wait_for_function("server.pending.length === 1")
    page.locator('[data-action="save-notes"]').click()
    page.wait_for_function("window.lastAlert === 'Notes saved.'")
    page.evaluate("server.pending[0].resolve(response({notes:'Stale stored notes'}))")
    expect(page.locator('[data-action="select-candidate"]')).to_have_count(3)
    expect(page.locator("#pick-notes")).to_have_value("Saved during refresh")


def test_keyboard_panel_navigation(page: Page) -> None:
    tabs = page.locator("#tabs button")
    expect(tabs).to_have_count(5)
    tabs.nth(0).focus()
    for index, name in enumerate(["overview", "draft", "review", "pick", "produce"]):
        if index:
            page.keyboard.press("Tab")
        expect(tabs.nth(index)).to_be_focused()
        page.keyboard.press("Enter" if index % 2 else "Space")
        expect(tabs.nth(index)).to_have_attribute("aria-pressed", "true")
        expect(page.locator(f"#panel-{name}")).to_be_visible()
        assert page.locator(".tab-panel:not([hidden])").count() == 1
    page.keyboard.press("Shift+Tab")
    expect(tabs.nth(3)).to_be_focused()
    tabs.nth(1).click()
    expect(tabs.nth(1)).to_be_focused()
    expect(page.locator("#panel-draft")).to_be_visible()


def test_pick_focus_after_select_tag_switch_and_delete(page: Page) -> None:
    pick(page)
    select = page.locator('[data-action="select-candidate"][data-candidate-id="c1"]')
    select.focus()
    select.press("Enter")
    page.wait_for_function("server.requests.some(r => r.key === 'POST /api/select')")
    page.wait_for_timeout(50)
    expect(select).to_be_focused()
    sidebar = page.locator('#pick-sidebar [data-asset-id="cms_tags"]')
    sidebar.focus()
    sidebar.press("Enter")
    expect(page.locator("#pick-tag-input")).to_be_visible()
    expect(sidebar).to_be_focused()
    tag = page.locator('[data-action="toggle-tag"]')
    tag.focus()
    tag.press("Space")
    expect(tag).to_have_attribute("aria-pressed", "true")
    expect(tag).to_be_focused()
    page.locator('#pick-sidebar [data-asset-id="title_detail"]').click()
    delete = page.locator('[data-action="delete-candidate"][data-candidate-id="c1"]')
    delete.focus()
    delete.press("Enter")
    expect(delete).to_have_count(0)
    successor = page.locator('[data-action="delete-candidate"][data-candidate-id="c2"]')
    expect(successor).to_be_focused()
    successor.press("Enter")
    expect(page.locator("#pick-asset-heading")).to_have_text("description")
    expect(page.locator("#pick-asset-heading")).to_be_focused()


def test_background_refresh_preserves_latest_focus_and_caret(page: Page) -> None:
    pick(page)
    notes = page.locator("#pick-notes")
    notes.fill("Draft")
    page.evaluate("server.holds = ['GET /api/assets/title_detail/notes']; void loadAssets()")
    page.wait_for_function("server.pending.length === 1")
    notes.press("End")
    notes.press("!")
    page.evaluate("server.pending[0].resolve(response({notes:'Stored'}))")
    expect(notes).to_have_value("Draft!")
    expect(notes).to_be_focused()
    assert notes.evaluate("el => el.selectionStart") == 6
    page.evaluate("void loadAssets()")
    page.wait_for_function("server.pending.length === 2")
    sidebar = page.locator('#pick-sidebar [data-asset-id="description"]')
    sidebar.click()
    expect(notes).to_have_attribute("data-asset-id", "description")
    page.evaluate("server.pending[1].resolve(response({notes:'Stale'}))")
    page.wait_for_timeout(30)
    expect(sidebar).to_be_focused()
    expect(notes).to_have_attribute("data-asset-id", "description")


@pytest.mark.parametrize(
    "asset,input_id,key",
    [
        ("title_detail", "pick-refine-instruction", "POST /api/assets/title_detail/refine"),
        ("cms_tags", "pick-tag-input", "PUT /api/assets/cms_tags/tags"),
    ],
)
def test_composition_enter_does_not_submit(page: Page, asset: str, input_id: str, key: str) -> None:
    pick(page, asset)
    control = page.locator(f"#{input_id}")
    control.fill("Python")
    for properties in [{"isComposing": True}, {"keyCode": 229}]:
        prevented = control.evaluate(
            """(el, properties) => {
          el.dispatchEvent(new CompositionEvent('compositionstart', {bubbles:true}));
          const event = new KeyboardEvent('keydown', {key:'Enter', bubbles:true, cancelable:true, ...properties});
          el.dispatchEvent(event);
          return event.defaultPrevented;
        }""",
            properties,
        )
        assert prevented is False
    control.evaluate("""el => {
      el.addEventListener('keydown', event => event.preventDefault(), {once:true});
      el.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true, cancelable:true}));
    }""")
    assert page.evaluate("key => server.requests.filter(r => r.key === key).length", key) == 0
    control.evaluate("el => el.dispatchEvent(new CompositionEvent('compositionend', {bubbles:true}))")
    control.press("Enter")
    page.wait_for_function("key => server.requests.filter(r => r.key === key).length === 1", arg=key)
