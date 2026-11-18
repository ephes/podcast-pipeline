"""Per-run session token of the local dashboard and pick UIs."""

from __future__ import annotations

import http.client
import json
import threading
import webbrowser
from collections.abc import Generator, Iterator
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from podcast_pipeline import local_web_guard
from podcast_pipeline.dashboard_context import DashboardContext
from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace
from podcast_pipeline.entrypoints import dashboard_web, pick_web
from podcast_pipeline.entrypoints.pick_web import _PickWebHandler, _ServerContext
from podcast_pipeline.local_web_guard import (
    SESSION_BOOTSTRAP_PAGE,
    SESSION_HEADER,
    SESSION_REJECTION_MESSAGE,
    browser_launch_file,
    check_session,
    new_session_token,
    session_bootstrap_cookie,
    session_cookie_name,
    session_url,
)
from podcast_pipeline.pick_core import load_candidates
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore
from tests.session_support import SESSION_TOKEN, launch_target

_PORT = 8765
_BASE = f"http://127.0.0.1:{_PORT}"
_JSON = {"Content-Type": "application/json"}
_COOKIE = session_cookie_name(_PORT)


# --- guard functions -----------------------------------------------------------


def test_new_session_tokens_are_long_and_distinct() -> None:
    tokens = {new_session_token() for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(token) >= 40 for token in tokens)


def test_session_url_carries_token() -> None:
    assert session_url("http://127.0.0.1:1/", "abc") == "http://127.0.0.1:1/?token=abc"


def test_browser_launch_file_is_private_and_removed() -> None:
    import os
    import stat
    from urllib.request import url2pathname

    url = 'http://127.0.0.1:1/?token=abc&x="<b>'
    with browser_launch_file(url) as launch_uri:
        path = Path(url2pathname(urlsplit(launch_uri).path))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
        assert launch_target(launch_uri) == url
        assert "<b>" not in path.read_text(encoding="utf-8")
    assert not path.exists()
    assert not path.parent.exists()


class _FakeController:
    def __init__(self, result: bool | Exception) -> None:
        self.result = result
        self.opened: list[str] = []

    def open(self, url: str) -> bool:
        self.opened.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_url_safe_controller_gets_the_url_directly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import tempfile

    controller = _FakeController(True)
    fallback: list[str] = []
    monkeypatch.setattr(local_web_guard, "_url_safe_controller", lambda: controller)
    monkeypatch.setattr("webbrowser.open", fallback.append)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with local_web_guard.open_in_browser("http://127.0.0.1:1/?token=abc"):
        assert controller.opened == ["http://127.0.0.1:1/?token=abc"]
    assert fallback == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("result", [False, OSError("synthetic"), webbrowser.Error("synthetic")])
def test_failed_url_safe_controller_falls_back_to_launch_file(
    monkeypatch: pytest.MonkeyPatch, result: bool | Exception
) -> None:
    controller = _FakeController(result)
    fallback: list[str] = []
    monkeypatch.setattr(local_web_guard, "_url_safe_controller", lambda: controller)
    monkeypatch.setattr("webbrowser.open", fallback.append)
    with local_web_guard.open_in_browser("http://127.0.0.1:1/?token=abc"):
        (launch_uri,) = fallback
        assert launch_uri.startswith("file://") and "abc" not in launch_uri
        assert launch_target(launch_uri) == "http://127.0.0.1:1/?token=abc"


@pytest.mark.parametrize("error", [OSError("synthetic pipe failure"), webbrowser.Error("synthetic")])
def test_failed_launch_keeps_serving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    controller = _FakeController(error)
    monkeypatch.setattr(local_web_guard, "_url_safe_controller", lambda: controller)

    def failing_open(_uri: str) -> bool:
        raise error  # CPython's webbrowser.open retries the same controller first

    monkeypatch.setattr("webbrowser.open", failing_open)
    entered = False
    with local_web_guard.open_in_browser("http://127.0.0.1:1/?token=abc"):
        entered = True
    assert entered
    assert "open the URL printed above" in capsys.readouterr().err


def test_launcher_returning_false_reports_and_keeps_serving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("webbrowser.open", lambda _uri: False)
    with local_web_guard.open_in_browser("http://127.0.0.1:1/?token=abc"):
        pass
    assert "open the URL printed above" in capsys.readouterr().err


def test_launch_file_creation_failure_reports_and_keeps_serving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import tempfile

    opened: list[str] = []

    def no_tempdir(*_args: object, **_kwargs: object) -> str:
        raise OSError("synthetic: no space left")

    monkeypatch.setattr(tempfile, "mkdtemp", no_tempdir)
    monkeypatch.setattr("webbrowser.open", opened.append)
    entered = False
    with local_web_guard.open_in_browser("http://127.0.0.1:1/?token=abc"):
        entered = True
    assert entered and opened == []
    assert "open the URL printed above" in capsys.readouterr().err


def test_body_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("webbrowser.open", lambda _uri: True)
    with pytest.raises(OSError, match="from the server"), local_web_guard.open_in_browser("http://127.0.0.1:1/"):
        raise OSError("from the server")


def test_run_pick_web_serves_when_no_browser_opens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pick_workspace(tmp_path)
    monkeypatch.setattr(pick_web, "_SHUTDOWN_TIMEOUT_SECONDS", 10)
    printed: dict[str, str] = {}
    clients: list[threading.Thread] = []

    def failing_open(_uri: str) -> bool:
        raise webbrowser.Error("no browser")

    real_print = print

    def capture_print(*args: Any, **kwargs: Any) -> None:
        text = " ".join(str(arg) for arg in args)
        if text.startswith("Pick UI: "):
            printed["url"] = text.removeprefix("Pick UI: ")
            clients.append(threading.Thread(target=finish))
            clients[-1].start()
        real_print(*args, **kwargs)

    results: dict[str, int] = {}

    def finish() -> None:
        parts = urlsplit(printed["url"])
        (token,) = parse_qs(parts.query)["token"]
        port = int(parts.port or 0)
        for _ in range(100):
            try:
                headers = {**_JSON, "Origin": f"http://127.0.0.1:{port}", SESSION_HEADER: token}
                results["done"] = _pick(port, "POST", "/api/done", headers, b"{}")[0]
                return
            except ConnectionError:
                threading.Event().wait(0.05)

    monkeypatch.setattr("webbrowser.open", failing_open)
    monkeypatch.setattr(pick_web, "print", capture_print, raising=False)
    pick_web.run_pick_web(workspace=tmp_path, asset_id=None)
    for client in clients:
        client.join(5)
    assert results == {"done": 200}


def test_other_controllers_get_a_launch_file(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    with local_web_guard.open_in_browser("http://127.0.0.1:1/?token=abc"):
        (launch_uri,) = opened
        assert launch_uri.startswith("file://") and "abc" not in launch_uri
        assert launch_target(launch_uri) == "http://127.0.0.1:1/?token=abc"


def test_browser_controller_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the autouse fixture replaced the function under test

    class FakeOSAScript(webbrowser.BaseBrowser):
        def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
            raise AssertionError("detection must not open a browser")

    osascript = FakeOSAScript("default")
    monkeypatch.setattr(webbrowser, "MacOSXOSAScript", FakeOSAScript, raising=False)
    monkeypatch.setattr(webbrowser, "get", lambda: osascript)
    assert local_web_guard._url_safe_controller() is osascript
    monkeypatch.setattr(webbrowser, "get", lambda: webbrowser.GenericBrowser(["xdg-open", "%s"]))
    assert local_web_guard._url_safe_controller() is None

    def no_browser() -> None:
        raise webbrowser.Error("none")

    monkeypatch.setattr(webbrowser, "get", no_browser)
    assert local_web_guard._url_safe_controller() is None
    monkeypatch.delattr(webbrowser, "MacOSXOSAScript", raising=False)
    assert local_web_guard._url_safe_controller() is None


def test_browser_launch_file_is_removed_on_error() -> None:
    from urllib.request import url2pathname

    with pytest.raises(RuntimeError), browser_launch_file("http://127.0.0.1:1/?token=abc") as launch_uri:
        path = Path(url2pathname(urlsplit(launch_uri).path))
        raise RuntimeError("synthetic")
    assert not path.parent.exists()


@pytest.mark.parametrize(
    ("cookie", "header", "ok"),
    [
        (None, SESSION_TOKEN, True),
        (f"{_COOKIE}={SESSION_TOKEN}", None, True),
        (f"other=1; {_COOKIE}={SESSION_TOKEN}; x=y", None, True),
        (f"{_COOKIE}=wrong; {_COOKIE}={SESSION_TOKEN}", None, True),
        (None, None, False),
        ("", "", False),
        (f"{_COOKIE}=wrong", "wrong", False),
        (f"{_COOKIE}=", None, False),
        (f"{session_cookie_name(_PORT + 1)}={SESSION_TOKEN}", None, False),
        (f"{_COOKIE}x={SESSION_TOKEN}", None, False),
        (None, SESSION_TOKEN + "x", False),
        (None, "tökén\udcff", False),
        (f"{_COOKIE}=tökén", None, False),
    ],
)
def test_check_session(cookie: str | None, header: str | None, ok: bool) -> None:
    rejection = check_session(token=SESSION_TOKEN, cookie_header=cookie, header_token=header, port=_PORT)
    assert (rejection is None) is ok
    if rejection is not None:
        assert rejection.status == 403
        assert rejection.message == SESSION_REJECTION_MESSAGE


def test_empty_server_token_admits_nothing() -> None:
    assert check_session(token="", cookie_header=f"{_COOKIE}=", header_token="", port=_PORT) is not None


@pytest.mark.parametrize(
    ("method", "path", "query", "ok"),
    [
        ("GET", "/", f"token={SESSION_TOKEN}", True),
        ("get", "/", f"token={SESSION_TOKEN}", True),
        ("POST", "/", f"token={SESSION_TOKEN}", False),
        ("GET", "/api/status", f"token={SESSION_TOKEN}", False),
        ("GET", "/", "token=wrong", False),
        ("GET", "/", "", False),
        ("GET", "/", f"token={SESSION_TOKEN}&token={SESSION_TOKEN}", False),
        ("GET", "/", "token=%ff%fe", False),
    ],
)
def test_session_bootstrap_cookie(method: str, path: str, query: str, ok: bool) -> None:
    cookie = session_bootstrap_cookie(method=method, path=path, query_string=query, token=SESSION_TOKEN, port=_PORT)
    if ok:
        assert cookie == f"{_COOKIE}={SESSION_TOKEN}; Path=/; HttpOnly; SameSite=Strict"
    else:
        assert cookie is None


# --- dashboard -----------------------------------------------------------------


@pytest.fixture()
def dashboard_app(tmp_path: Path) -> tuple[Any, list[str]]:
    EpisodeWorkspaceStore(tmp_path).write_episode_yaml({"episode_id": "ep1"})
    done: list[str] = []
    app = dashboard_web.create_dashboard_app(
        ctx=DashboardContext(workspace=tmp_path),
        on_done=lambda: done.append("done"),
        port=_PORT,
        session_token=SESSION_TOKEN,
    )
    return app, done


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/"), ("GET", "/api/status"), ("GET", "/api/jobs"), ("POST", "/api/done"), ("POST", "/api/produce")],
)
def test_dashboard_requires_session_token(dashboard_app: tuple[Any, list[str]], method: str, path: str) -> None:
    app, done = dashboard_app
    client = TestClient(app, base_url=_BASE)
    response = client.request(method, path, headers=_JSON, content=b"{}" if method == "POST" else None)
    assert response.status_code == 403
    assert response.json() == {"error": SESSION_REJECTION_MESSAGE}
    assert response.headers["X-Frame-Options"] == "DENY"
    assert done == []


def test_dashboard_wrong_token_is_rejected(dashboard_app: tuple[Any, list[str]]) -> None:
    app, _done = dashboard_app
    client = TestClient(app, base_url=_BASE)
    assert client.get("/api/status", headers={SESSION_HEADER: "wrong"}).status_code == 403
    assert client.get("/", params={"token": "wrong"}, follow_redirects=False).status_code == 403
    client.cookies.set(session_cookie_name(_PORT + 1), SESSION_TOKEN)
    assert client.get("/api/status").status_code == 403


def test_dashboard_bootstrap_sets_cookie_and_forwards(dashboard_app: tuple[Any, list[str]]) -> None:
    app, done = dashboard_app
    client = TestClient(app, base_url=_BASE)
    response = client.get("/", params={"token": SESSION_TOKEN}, follow_redirects=False)
    assert response.status_code == 200
    assert response.content == SESSION_BOOTSTRAP_PAGE
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    set_cookie = response.headers["Set-Cookie"]
    assert set_cookie == f"{_COOKIE}={SESSION_TOKEN}; Path=/; HttpOnly; SameSite=Strict"
    assert SESSION_TOKEN not in response.text

    # The client keeps the cookie: the clean URL and the API now work without the token.
    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 200
    posted = client.post("/api/done", headers={**_JSON, "Origin": _BASE}, content=b"{}")
    assert posted.status_code == 200
    assert done == ["done"]


def test_dashboard_bootstrap_still_checks_host(dashboard_app: tuple[Any, list[str]]) -> None:
    app, _done = dashboard_app
    client = TestClient(app, base_url=f"http://evil.example:{_PORT}")
    response = client.get("/", params={"token": SESSION_TOKEN}, follow_redirects=False)
    assert response.status_code == 403
    assert "Set-Cookie" not in response.headers


def test_dashboard_header_token_is_accepted(dashboard_app: tuple[Any, list[str]]) -> None:
    app, _done = dashboard_app
    client = TestClient(app, base_url=_BASE)
    assert client.get("/api/status", headers={SESSION_HEADER: SESSION_TOKEN}).status_code == 200


def test_run_dashboard_opens_tokenized_url_per_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    EpisodeWorkspaceStore(tmp_path).write_episode_yaml({"episode_id": "ep1"})
    opened: list[str] = []
    apps: list[Any] = []

    def browser(launch_uri: str) -> None:
        assert "token" not in launch_uri
        opened.append(launch_target(launch_uri))

    monkeypatch.setattr("webbrowser.open", browser)
    monkeypatch.setattr(dashboard_web, "_run_uvicorn_server", lambda server, _sock: apps.append(server.config.app))

    dashboard_web.run_dashboard(workspace=tmp_path)
    dashboard_web.run_dashboard(workspace=tmp_path)

    tokens = []
    for url, app in zip(opened, apps, strict=True):
        parts = urlsplit(url)
        assert parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.path == "/"
        (token,) = parse_qs(parts.query)["token"]
        tokens.append(token)
        client = TestClient(app, base_url=f"http://127.0.0.1:{parts.port}")
        assert client.get("/api/status").status_code == 403
        assert client.get("/api/status", headers={SESSION_HEADER: token}).status_code == 200
    assert len(set(tokens)) == 2


# --- pick UI -------------------------------------------------------------------


@pytest.fixture()
def pick_port(tmp_path: Path) -> Generator[int, None, None]:
    _pick_workspace(tmp_path)
    store = EpisodeWorkspaceStore(tmp_path)
    ctx = _ServerContext(
        store=store,
        candidates_by_asset=load_candidates(layout=store.layout, asset_id=None),
        workspace_state=EpisodeWorkspace(episode_id="ep1", root_dir="."),
        session_token=SESSION_TOKEN,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_PickWebHandler, ctx))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield int(server.server_address[1])
    server.shutdown()
    server.server_close()


def _pick(
    port: int, method: str, path: str, headers: dict[str, str] | None = None, body: bytes | None = None
) -> tuple[int, Any, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.headers, response.read()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/"), ("GET", "/api/assets"), ("POST", "/api/done"), ("POST", "/api/select")],
)
def test_pick_requires_session_token(pick_port: int, method: str, path: str) -> None:
    headers = {**_JSON, "Origin": f"http://127.0.0.1:{pick_port}"} if method == "POST" else {}
    status, response_headers, body = _pick(pick_port, method, path, headers, b"{}" if method == "POST" else None)
    assert status == 403
    assert json.loads(body) == {"error": SESSION_REJECTION_MESSAGE}
    assert response_headers["X-Frame-Options"] == "DENY"


def test_pick_wrong_token_is_rejected(pick_port: int) -> None:
    assert _pick(pick_port, "GET", "/api/assets", {SESSION_HEADER: "wrong"})[0] == 403
    assert _pick(pick_port, "GET", "/?token=wrong")[0] == 403
    other_cookie = {"Cookie": f"{session_cookie_name(pick_port + 1)}={SESSION_TOKEN}"}
    assert _pick(pick_port, "GET", "/api/assets", other_cookie)[0] == 403


def test_pick_bootstrap_sets_cookie_and_forwards(pick_port: int) -> None:
    status, headers, body = _pick(pick_port, "GET", f"/?token={SESSION_TOKEN}")
    assert status == 200
    assert body == SESSION_BOOTSTRAP_PAGE
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"
    cookie = f"{session_cookie_name(pick_port)}={SESSION_TOKEN}"
    assert headers["Set-Cookie"] == f"{cookie}; Path=/; HttpOnly; SameSite=Strict"

    assert _pick(pick_port, "GET", "/", {"Cookie": cookie})[0] == 200
    assert _pick(pick_port, "GET", "/api/assets", {"Cookie": cookie})[0] == 200
    assert _pick(pick_port, "GET", "/api/assets", {SESSION_HEADER: SESSION_TOKEN})[0] == 200


@pytest.mark.parametrize("method", ["OPTIONS", "HEAD", "PUT", "PATCH", "DELETE", "TRACE"])
def test_pick_guards_every_method_before_dispatch(pick_port: int, method: str) -> None:
    # Same-origin JSON, so only the session check can refuse it (not the Origin/Content-Type guard).
    headers = {**_JSON, "Origin": f"http://127.0.0.1:{pick_port}"}
    status, headers, body = _pick(pick_port, method, "/api/assets", headers, b"{}")
    assert status == 403
    assert headers["X-Frame-Options"] == "DENY"
    if method == "HEAD":
        assert body == b""
    else:
        assert json.loads(body) == {"error": SESSION_REJECTION_MESSAGE}


def test_pick_head_rejection_has_no_body(pick_port: int) -> None:
    import socket

    with socket.create_connection(("127.0.0.1", pick_port), timeout=5) as sock:
        sock.sendall(f"HEAD /api/assets HTTP/1.0\r\nHost: 127.0.0.1:{pick_port}\r\n\r\n".encode())
        raw = b""
        while chunk := sock.recv(4096):
            raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.split(b"\r\n", 1)[0].split()[1] == b"403"
    assert body == b""


@pytest.mark.parametrize("target", ["//[", "/?token=%", "/?" + "token=" + SESSION_TOKEN + "#x", "/%zz?token=["])
def test_pick_malformed_targets_get_a_response(pick_port: int, target: str) -> None:
    status, _headers, _body = _pick(pick_port, "GET", target)
    assert status in {403, 404}


def test_pick_bootstrap_still_checks_host(pick_port: int) -> None:
    status, headers, _body = _pick(pick_port, "GET", f"/?token={SESSION_TOKEN}", {"Host": f"evil.example:{pick_port}"})
    assert status == 403
    assert headers["Set-Cookie"] is None


def _pick_workspace(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep1"})
    store.write_candidate(Candidate(asset_id="description", content="# Description\n\nSynthetic."))


def test_run_pick_web_opens_tokenized_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pick_workspace(tmp_path)
    results: dict[str, int] = {}
    clients: list[threading.Thread] = []
    monkeypatch.setattr(pick_web, "_SHUTDOWN_TIMEOUT_SECONDS", 10)

    def open_browser(launch_uri: str) -> None:
        assert "token" not in launch_uri
        parts = urlsplit(launch_target(launch_uri))
        assert parts.hostname == "127.0.0.1" and parts.path == "/"
        (token,) = parse_qs(parts.query)["token"]
        assert len(token) >= 40

        def drive() -> None:
            port = int(parts.port or 0)
            results["anonymous"] = _pick(port, "GET", "/api/assets")[0]
            done_headers = {**_JSON, "Origin": f"http://127.0.0.1:{port}", SESSION_HEADER: token}
            results["done"] = _pick(port, "POST", "/api/done", done_headers, b"{}")[0]

        client = threading.Thread(target=drive)
        clients.append(client)
        client.start()

    monkeypatch.setattr("webbrowser.open", open_browser)
    pick_web.run_pick_web(workspace=tmp_path, asset_id=None)
    for client in clients:
        client.join(5)
    assert results == {"anonymous": 403, "done": 200}


# --- real browser ----------------------------------------------------------------


@pytest.fixture()
def chromium() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover - depends on the local browser install
            pytest.skip(f"Chromium unavailable: {exc}")
        try:
            yield browser
        finally:
            browser.close()


def test_browser_session_flow_against_real_pick_server(chromium: Any, tmp_path: Path) -> None:
    _pick_workspace(tmp_path)
    store = EpisodeWorkspaceStore(tmp_path)
    ctx = _ServerContext(
        store=store,
        candidates_by_asset=load_candidates(layout=store.layout, asset_id=None),
        workspace_state=EpisodeWorkspace(episode_id="ep1", root_dir="."),
        session_token=SESSION_TOKEN,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_PickWebHandler, ctx))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = int(server.server_address[1])
    try:
        stranger = chromium.new_page()
        assert stranger.goto(f"http://127.0.0.1:{port}/").status == 403
        stranger.close()

        page = chromium.new_page()
        with browser_launch_file(session_url(f"http://127.0.0.1:{port}/", SESSION_TOKEN)) as launch_uri:
            page.goto(launch_uri)
            page.wait_for_url(f"http://127.0.0.1:{port}/")
        page.wait_for_function("document.body.innerText.includes('Synthetic')")
        assert page.evaluate("document.cookie") == ""  # HttpOnly
        assets = page.evaluate("fetch('/api/assets').then(r => r.status)")
        assert assets == 200
    finally:
        server.shutdown()
        server.server_close()


def test_browser_session_flow_against_real_dashboard_server(chromium: Any, tmp_path: Path) -> None:
    import socket

    import uvicorn

    EpisodeWorkspaceStore(tmp_path).write_episode_yaml({"episode_id": "synthetic-ep"})
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = int(sock.getsockname()[1])
    app = dashboard_web.create_dashboard_app(
        ctx=DashboardContext(workspace=tmp_path), port=port, session_token=SESSION_TOKEN
    )
    server = uvicorn.Server(uvicorn.Config(app=app, access_log=False, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        page = chromium.new_page()
        for _ in range(100):
            if server.started:
                break
            page.wait_for_timeout(50)
        stranger = chromium.new_page()
        assert stranger.goto(f"http://127.0.0.1:{port}/api/episode").status == 403
        stranger.close()

        with browser_launch_file(session_url(f"http://127.0.0.1:{port}/", SESSION_TOKEN)) as launch_uri:
            page.goto(launch_uri)
            page.wait_for_url(f"http://127.0.0.1:{port}/")
        page.wait_for_load_state()
        assert SESSION_REJECTION_MESSAGE not in page.content()
        assert page.evaluate("fetch('/api/episode').then(r => r.json())")["episode_id"] == "synthetic-ep"
        # The printed URL opened directly (no launch file) works too.
        direct = chromium.new_page()
        direct.goto(session_url(f"http://127.0.0.1:{port}/", SESSION_TOKEN))
        direct.wait_for_url(f"http://127.0.0.1:{port}/")
        assert direct.evaluate("fetch('/api/status').then(r => r.status)") == 200
        assert page.evaluate("document.cookie") == ""
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
