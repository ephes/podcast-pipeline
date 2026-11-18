from __future__ import annotations

import urllib.request
from collections.abc import Iterator

import pytest

from podcast_pipeline import local_web_guard
from podcast_pipeline.local_web_guard import SESSION_HEADER
from tests.session_support import SESSION_TOKEN


@pytest.fixture(autouse=True)
def _no_real_browser_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    """UI launches in tests go through the (patched) ``webbrowser.open`` with a launch file.

    Without this, macOS's osascript controller would open a real browser window.
    """
    monkeypatch.setattr(local_web_guard, "_url_safe_controller", lambda: None)


@pytest.fixture(autouse=True)
def _urllib_sends_session_token() -> Iterator[None]:
    """Plain ``urllib`` calls in the UI tests carry the shared test session token.

    The servers under test are created with ``SESSION_TOKEN``; tests of the token
    check itself use ``http.client`` or explicit clients without it.
    """
    previous = getattr(urllib.request, "_opener", None)
    opener = urllib.request.build_opener()
    opener.addheaders = [*opener.addheaders, (SESSION_HEADER, SESSION_TOKEN)]
    urllib.request.install_opener(opener)
    try:
        yield
    finally:
        urllib.request.install_opener(previous)  # type: ignore[arg-type]
