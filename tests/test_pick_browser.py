"""Exercise the shipped standalone picker with synthetic exchanges only."""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect, sync_playwright

from podcast_pipeline.entrypoints.pick_web import _build_html_page

FAKE_API = r"""
window.server = {
  assets: ['description', 'shownotes', 'cms_tags'].map(asset_id => ({
    asset_id, candidates: ['c1', 'c2'].map(candidate_id => ({
      candidate_id, format: 'markdown', content: 'Synthetic copy', content_html: '<p>Synthetic copy</p>'
    })), is_tag_asset: asset_id === 'cms_tags', selected_tags: [],
    tag_pool: [{tag:'Python', count:2}], max_tags:12, selection_format:'markdown', has_selection:true
  })), requests: [], pending: [], hold: '', bodyHold: '', fail: ''
};
window.response = data => ({ok:true, json:async () => data});
window.fetch = async (path, options = {}) => {
  const body = options.body ? JSON.parse(options.body) : {};
  server.requests.push({path, body});
  if (server.fail === path) throw Error('Synthetic failure');
  if (server.hold === path) return new Promise(resolve => server.pending.push({resolve, body}));
  if (server.bodyHold === path) return {
    ok:true, json:() => new Promise(resolve => server.pending.push({resolve, body}))
  };
  if (path === '/api/assets') return response(server.assets);
  if (path === '/api/select-custom') return response({text:body.text});
  if (path === '/api/tags') return response({tags:body.tags});
  if (path === '/api/refine') return response({candidate:{candidate_id:'c3',content:'Refined',format:'markdown'}});
  return response({ok:true});
};
window.alert = message => window.lastAlert = message;
"""


@pytest.fixture
def page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.add_init_script(FAKE_API)
        page.route(
            "http://picker.test/", lambda route: route.fulfill(body=_build_html_page(), content_type="text/html")
        )
        page.goto("http://picker.test/")
        expect(page.locator("#custom-text")).to_be_visible()
        yield page
        browser.close()


@pytest.mark.parametrize("phase", ["hold", "bodyHold"])
@pytest.mark.parametrize("operation", ["select", "select-custom", "refine", "tags", "assets", "done"])
def test_whole_exchange_deadline(page: Page, phase: str, operation: str) -> None:
    page.clock.install()
    page.evaluate(f"server.{phase} = '/api/{operation}'")
    action = {
        "select": "handleSelect('description', 'c1')",
        "select-custom": "handleSaveCustom('description')",
        "refine": "handleRefine('description')",
        "tags": "saveTags(assets[2], ['Python'])",
        "assets": "init()",
        "done": "handleDone()",
    }[operation]
    page.evaluate(f"window.settled = false; Promise.resolve({action}).catch(() => {{}}).finally(() => settled = true)")
    page.wait_for_function("server.pending.length === 1")
    page.clock.fast_forward(30001)
    page.wait_for_function("settled")
    if operation == "done":
        expect(page.locator("#done-btn")).to_be_visible()
    if operation == "refine":
        expect(page.locator("#refine-btn")).to_be_enabled()


def test_selection_has_one_pending_choice(page: Page) -> None:
    page.evaluate("server.hold = '/api/select'; handleSelect('description','c1'); handleSelect('description','c2')")
    page.wait_for_function("server.pending.length >= 1")
    assert page.evaluate("server.requests.filter(r => r.path === '/api/select').length") == 1
    page.evaluate("server.pending[0].resolve(response({ok:true}))")
    page.wait_for_function("assets[0].selected_candidate_id === 'c1'")
    expect(page.locator("#card-c1")).to_have_class("candidate-card selected")


@pytest.mark.parametrize("failed", [False, True])
def test_custom_ack_preserves_newer_draft_and_other_asset(page: Page, failed: bool) -> None:
    page.locator("#custom-text").fill("Submitted A")
    page.evaluate("server.hold = '/api/select-custom'")
    page.locator("#custom-save").click()
    page.wait_for_function("server.pending.length === 1")
    page.locator("#custom-text").fill("Newer B")
    page.evaluate("selectAsset('shownotes')")
    page.locator("#custom-text").fill("Other editor")
    page.evaluate(
        "server.pending[0].resolve("
        + ("{ok:false,json:async () => ({error:'Save failed'})}" if failed else "response({text:'Submitted A'})")
        + ")"
    )
    page.wait_for_function("!pendingChoices.has('description')")
    expect(page.locator("#custom-text")).to_have_value("Other editor")
    expect(page.locator("#custom-error")).to_be_empty()
    page.evaluate("selectAsset('description')")
    expect(page.locator("#custom-text")).to_have_value("Newer B")


def test_native_sidebar_and_focus_continuity(page: Page) -> None:
    sidebar = page.locator("#sidebar button")
    expect(sidebar).to_have_count(3)
    sidebar.nth(1).focus()
    page.keyboard.press("Enter")
    expect(sidebar.nth(1)).to_be_focused()
    expect(sidebar.nth(1)).to_have_attribute("aria-pressed", "true")
    select = page.locator("#card-c1 .select-btn")
    select.focus()
    page.keyboard.press("Space")
    page.wait_for_function("assets[1].selected_candidate_id === 'c1'")
    expect(select).to_be_focused()
    page.evaluate("selectAsset('cms_tags')")
    chip = page.locator(".tag-chip").first
    chip.focus()
    page.keyboard.press("Space")
    page.wait_for_function("assets[2].selected_tags.length === 1")
    expect(chip).to_be_focused()


def test_background_save_preserves_caret(page: Page) -> None:
    page.evaluate("server.hold = '/api/select-custom'")
    page.locator("#custom-text").fill("A")
    page.locator("#custom-save").click()
    page.locator("#custom-text").fill("New draft")
    page.locator("#custom-text").evaluate("(el) => el.setSelectionRange(2, 5)")
    page.evaluate("server.pending[0].resolve(response({text:'A'}))")
    page.wait_for_function("!pendingChoices.has('description')")
    expect(page.locator("#custom-text")).to_be_focused()
    assert page.locator("#custom-text").evaluate("(el) => [el.selectionStart, el.selectionEnd]") == [2, 5]


@pytest.mark.parametrize(
    "key",
    [
        {"key": "Enter", "isComposing": True},
        {"key": "Enter", "keyCode": 229},
        {"key": "Enter", "cancelable": True, "handled": True},
    ],
)
@pytest.mark.parametrize("asset,input_id", [("description", "refine-instruction"), ("cms_tags", "tag-input")])
def test_composition_enter_does_not_submit(page: Page, key: dict[str, object], asset: str, input_id: str) -> None:
    page.evaluate("(id) => selectAsset(id)", asset)
    page.locator(f"#{input_id}").fill("Synthetic text")
    page.evaluate("server.requests = []")
    page.locator(f"#{input_id}").evaluate(
        """(el, props) => {
      const event = new KeyboardEvent('keydown', props);
      if (props.handled) event.preventDefault();
      el.dispatchEvent(event);
    }""",
        key,
    )
    assert page.evaluate("server.requests.filter(r => r.path !== '/api/assets').length") == 0
    page.locator(f"#{input_id}").press("Enter")
    page.wait_for_function("server.requests.some(r => r.path !== '/api/assets')")
    assert page.evaluate("server.requests.filter(r => r.path !== '/api/assets').length") == 1
