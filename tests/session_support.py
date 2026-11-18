"""Shared session token for tests that drive the local web UIs."""

from podcast_pipeline.local_web_guard import SESSION_HEADER

SESSION_TOKEN = "test-session-token-0123456789abcdef"
SESSION_HEADERS = {SESSION_HEADER: SESSION_TOKEN}


def launch_target(launch_uri: str) -> str:
    """Return the URL the browser was sent to: the forward target of a launch file, or the URL itself."""
    import html
    import re
    from pathlib import Path
    from urllib.parse import urlsplit
    from urllib.request import url2pathname

    parts = urlsplit(launch_uri)
    if parts.scheme == "http":  # macOS controller: the URL itself, handed over on stdin
        return launch_uri
    assert parts.scheme == "file", launch_uri
    page = Path(url2pathname(parts.path)).read_text(encoding="utf-8")
    match = re.search(r'http-equiv="refresh" content="0;url=([^"]+)"', page)
    assert match is not None, page
    return html.unescape(match.group(1))
