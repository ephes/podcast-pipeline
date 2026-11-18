"""Request guards for the local (127.0.0.1) web UIs.

The dashboard and the pick UI listen on loopback, but any web page the user
has open can still send requests to them, and DNS rebinding can make a
foreign hostname resolve to 127.0.0.1. These checks keep both UIs usable only
from their own pages:

* the ``Host`` header must name a loopback host (and the server's own port
  when it is known), which defeats DNS rebinding;
* state-changing requests (POST/PUT/PATCH/DELETE) must be same-origin: an
  ``Origin`` header must match ``http://<Host>``, and a ``Sec-Fetch-Site``
  header, when present, must be ``same-origin`` or ``none``;
* state-changing requests must declare ``Content-Type: application/json``.
  Browsers cannot send that cross-site without a CORS preflight, which these
  servers never approve, so ``text/plain`` or form posts from other sites are
  rejected even by browsers that omit ``Origin``.

Every request must also carry the per-run session token (``check_session``):
each server start draws a random token for ``http://127.0.0.1:<port>/?token=...``.
``open_in_browser`` never puts that URL into a process argument other users could list:
Python's macOS ``osascript`` controller, when selected, receives it on stdin; otherwise
the browser opens a private launch file (``browser_launch_file``) that forwards to it. The first request sets an
HttpOnly, SameSite=Strict cookie and answers a small page that forwards to ``/``
(``session_bootstrap_cookie``, ``SESSION_BOOTSTRAP_PAGE``); a redirect would lose the
Strict cookie after a launch from ``file://``. Afterwards the cookie (or an
``X-Session-Token`` header, for scripts) admits requests; requests without the
token are refused.

Every response, rejections included, also carries ``RESPONSE_SECURITY_HEADERS``:
other sites cannot frame the UIs (which would let them trick clicks on
same-origin buttons such as starting a paid production), and browsers do not
sniff content types.
"""

from __future__ import annotations

import hmac
import html
import os
import secrets
import shutil
import sys
import tempfile
import webbrowser
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})
_SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})

RESPONSE_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)

SESSION_HEADER = "X-Session-Token"
SESSION_QUERY_PARAM = "token"
_SESSION_COOKIE_PREFIX = "podcast_pipeline_session"
SESSION_REJECTION_MESSAGE = "Missing or invalid session token; open the URL printed in the terminal"
# Answer to the opening request: same-site forward to the clean URL, so the Strict cookie is sent.
SESSION_BOOTSTRAP_PAGE = (
    b'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/">'
    b'<title>podcast-pipeline</title><p><a href="/">Continue</a></p>'
)


@dataclass(frozen=True)
class GuardRejection:
    status: int
    message: str


def _split_host_header(host: str) -> tuple[str, int | None] | None:
    host = host.strip().lower()
    if not host:
        return None
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return None
        hostname, rest = host[: end + 1], host[end + 1 :]
    else:
        hostname, sep, port_text = host.partition(":")
        rest = f":{port_text}" if sep else ""
    if not rest:
        return hostname, None
    if not rest.startswith(":"):
        return None
    port_text = rest[1:]
    if not 1 <= len(port_text) <= 5 or not port_text.isascii() or not port_text.isdigit():
        return None
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None
    return hostname, port


def check_request(
    *,
    method: str,
    host: str | None,
    origin: str | None,
    sec_fetch_site: str | None,
    content_type: str | None,
    expected_port: int | None,
) -> GuardRejection | None:
    """Return a rejection for a request that fails the local-UI guards, else ``None``."""
    if host is None:
        return GuardRejection(403, "Missing Host header")
    parsed = _split_host_header(host)
    if parsed is None or parsed[0] not in _LOOPBACK_HOSTNAMES:
        return GuardRejection(403, "Forbidden host")
    if expected_port is not None and parsed[1] != expected_port:
        return GuardRejection(403, "Forbidden host")

    if method.upper() not in UNSAFE_METHODS:
        return None

    if origin is not None and origin.strip().lower() != f"http://{host.strip().lower()}":
        return GuardRejection(403, "Cross-origin request rejected")
    if sec_fetch_site is not None and sec_fetch_site.strip().lower() not in _SAME_ORIGIN_FETCH_SITES:
        return GuardRejection(403, "Cross-site request rejected")

    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        return GuardRejection(415, "Content-Type must be application/json")
    return None


def new_session_token() -> str:
    """Return a fresh random session token for one server run."""
    return secrets.token_urlsafe(32)


def session_cookie_name(port: int | None) -> str:
    """Cookie name for one server; cookies are not port-scoped, so the port keeps runs apart."""
    return _SESSION_COOKIE_PREFIX if port is None else f"{_SESSION_COOKIE_PREFIX}_{port}"


def session_url(base_url: str, token: str) -> str:
    """URL that opens the UI and starts the browser session (``base_url`` ends with ``/``)."""
    return f"{base_url}?{SESSION_QUERY_PARAM}={token}"


def _token_matches(candidate: str | None, token: str) -> bool:
    if not candidate or not token:
        return False
    return hmac.compare_digest(candidate.encode("utf-8", "surrogatepass"), token.encode("utf-8"))


def _cookie_values(cookie_header: str | None, name: str) -> list[str]:
    if not cookie_header:
        return []
    values = []
    for part in cookie_header.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            values.append(value.strip())
    return values


def check_session(
    *,
    token: str,
    cookie_header: str | None,
    header_token: str | None,
    port: int | None,
) -> GuardRejection | None:
    """Reject a request that carries neither the session cookie nor the session header."""
    if _token_matches(header_token, token):
        return None
    if any(_token_matches(value, token) for value in _cookie_values(cookie_header, session_cookie_name(port))):
        return None
    return GuardRejection(403, SESSION_REJECTION_MESSAGE)


@contextmanager
def browser_launch_file(url: str) -> Iterator[str]:
    """Yield the ``file://`` URI of a private page that forwards the browser to ``url``.

    Browser launchers get their URL as a process argument, which other local users
    may be able to list; the token stays in a ``0600`` file inside a ``0700``
    directory instead. The file is removed when the context ends.
    """
    directory = Path(tempfile.mkdtemp(prefix="podcast-pipeline-"))
    try:
        path = directory / "open.html"
        target = html.escape(url, quote=True)
        page = (
            f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={target}">'
            f'<title>podcast-pipeline</title><p><a href="{target}">Open</a></p>'
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(page)
        yield path.as_uri()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _url_safe_controller() -> webbrowser.BaseBrowser | None:
    """Return the selected ``webbrowser`` controller if it keeps the URL out of process arguments.

    macOS's ``MacOSXOSAScript`` writes an AppleScript to ``osascript``'s stdin and the
    browser receives the URL as an Apple Event. Giving it a ``file://`` URI instead would
    switch it to scripting the browser application (an Automation permission prompt), so
    it gets the plain URL. Command-line launchers (``xdg-open``, ``$BROWSER``) do not
    qualify, and which controller Python selects depends on the platform and version.
    """
    controller_type = getattr(webbrowser, "MacOSXOSAScript", None)
    if controller_type is None:
        return None
    try:
        controller = webbrowser.get()
    except webbrowser.Error:
        return None
    return controller if isinstance(controller, controller_type) else None


@contextmanager
def open_in_browser(url: str) -> Iterator[None]:
    """Open the tokenized ``url`` without exposing it in a process argument; keep any launch file until exit.

    Only the checked controller ever sees ``url`` (``webbrowser.open`` could fall back to
    command-line launchers); every other path opens the private launch file.
    """
    controller = _url_safe_controller()
    if controller is not None:
        try:
            opened = controller.open(url)
        except (webbrowser.Error, OSError):
            opened = False
        if opened:
            yield
            return
    with ExitStack() as stack:
        try:
            launch_uri = stack.enter_context(browser_launch_file(url))
            opened = bool(webbrowser.open(launch_uri))
        except (webbrowser.Error, OSError):
            opened = False
        if not opened:
            # Keep serving: the URL printed to the terminal still opens the UI.
            print("Could not open a browser; open the URL printed above.", file=sys.stderr)
        yield


def session_bootstrap_cookie(
    *,
    method: str,
    path: str,
    query_string: str,
    token: str,
    port: int | None,
) -> str | None:
    """Return the ``Set-Cookie`` value when this is the opening ``GET /?token=...`` request.

    The caller answers with ``SESSION_BOOTSTRAP_PAGE``, which forwards to ``/`` so the
    token leaves the address bar. Call it only for requests that already passed
    ``check_request``.
    """
    if method.upper() != "GET" or path != "/":
        return None
    try:
        values = parse_qs(query_string, keep_blank_values=True, strict_parsing=False).get(SESSION_QUERY_PARAM, [])
    except ValueError:
        return None
    if len(values) != 1 or not _token_matches(values[0], token):
        return None
    return f"{session_cookie_name(port)}={token}; Path=/; HttpOnly; SameSite=Strict"
