from __future__ import annotations

import gzip
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from podcast_pipeline import cms_examples
from podcast_pipeline.entrypoints.cli import app

_REDIRECTS = {
    "/redirect-large": "/list",
    "/redirect-compressed": "/list",
    "/redirect-loop": "/redirect-loop",
}


def _send_redirect(handler: BaseHTTPRequestHandler, path: str, sent: dict[str, int]) -> None:
    handler.send_response(302)
    handler.send_header("Location", _REDIRECTS[path])
    if path == "/redirect-compressed":
        handler.send_header("Content-Encoding", "gzip")
    handler.end_headers()
    sent[path] = 0
    try:
        if path == "/redirect-compressed":
            handler.wfile.write(gzip.compress(b"x" * (9 * 1024 * 1024)))
        elif path == "/redirect-large":
            for _ in range(32):
                time.sleep(0.01)
                handler.wfile.write(b"x" * 65536)
                handler.wfile.flush()
                sent[path] += 65536
    except (BrokenPipeError, ConnectionResetError):
        pass


def _send_body(handler: BaseHTTPRequestHandler, path: str, sent: dict[str, int]) -> None:
    item = {"id": 42, "title": "Episode", "description": "<p>Notes</p>"}
    if path in {"/large", "/error"}:
        for _ in range(32):
            time.sleep(0.01)
            handler.wfile.write(b"x" * 65536)
            handler.wfile.flush()
            sent[path] += 65536
    elif path == "/stall":
        handler.wfile.write(b"[")
        handler.wfile.flush()
        time.sleep(0.9)
    elif path == "/drip":
        for _ in range(30):
            time.sleep(0.03)
            handler.wfile.write(b" ")
            handler.wfile.flush()
        handler.wfile.write(json.dumps([item]).encode())
    else:
        payload: object = [item, item] if path == "/list" else {"items": [item, item]}
        body = json.dumps(payload).encode()
        if path == "/compressed":
            body = gzip.compress(body)
        elif path == "/invalid":
            body = b"not JSON"
        handler.wfile.write(body)


@pytest.fixture
def cms_server() -> Iterator[tuple[str, dict[str, int]]]:
    sent: dict[str, int] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if path == "/headers":
                time.sleep(0.6)
            status = 500 if path == "/error" else 200
            if path in _REDIRECTS:
                _send_redirect(self, path, sent)
                return
            self.send_response(status)
            if path == "/compressed":
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            sent[path] = 0
            try:
                _send_body(self, path, sent)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", sent
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("large", "byte limit"),
        ("error", "HTTP 500"),
        ("headers", "timeout"),
        ("stall", "timeout"),
        ("compressed", "Content-Encoding"),
        ("invalid", "invalid JSON"),
        ("redirect-loop", "redirected more than"),
    ],
)
def test_cms_fetch_failure_preserves_export(
    cms_server: tuple[str, dict[str, int]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    message: str,
) -> None:
    url, sent = cms_server
    monkeypatch.setattr(cms_examples, "_MAX_CMS_RESPONSE_BYTES", 128 * 1024, raising=False)
    output = tmp_path / "examples.jsonl"
    output.write_bytes(b"existing corpus\n")
    result = CliRunner().invoke(
        app,
        [
            "cms-examples",
            "--api-url",
            f"{url}/{path}",
            "--output",
            str(output),
            "--limit",
            "1",
            "--timeout-seconds",
            "0.3",
        ],
    )
    assert result.exit_code != 0
    assert message in result.output
    assert output.read_bytes() == b"existing corpus\n"
    assert list(tmp_path.iterdir()) == [output]
    if path in {"large", "error"}:
        assert sent[f"/{path}"] < 32 * 65536


@pytest.mark.parametrize("path", ["list", "wagtail"])
def test_cms_fetch_small_response_and_local_limit(cms_server: tuple[str, dict[str, int]], path: str) -> None:
    url, _ = cms_server
    examples = cms_examples.fetch_cms_examples(api_url=f"{url}/{path}", limit=1)
    assert len(examples) == 1
    assert examples[0].page_id == "42"


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_cms_fetch_requires_finite_positive_deadline(timeout: float) -> None:
    with pytest.raises(cms_examples.CmsExamplesError, match="finite"):
        cms_examples.fetch_cms_examples(api_url="https://invalid.example", limit=1, timeout_seconds=timeout)


@pytest.mark.parametrize("path", ["redirect-large", "redirect-compressed"])
def test_cms_fetch_follows_redirect_without_reading_its_body(
    cms_server: tuple[str, dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    url, sent = cms_server
    monkeypatch.setattr(cms_examples, "_MAX_CMS_RESPONSE_BYTES", 128 * 1024)

    def eager_read(self: httpx.Response) -> bytes:
        raise AssertionError("response body must only be read through the bounded stream")

    monkeypatch.setattr(httpx.Response, "read", eager_read)
    examples = cms_examples.fetch_cms_examples(api_url=f"{url}/{path}", limit=1, timeout_seconds=5)
    assert len(examples) == 1
    assert examples[0].page_id == "42"
    if path == "redirect-large":
        assert sent[f"/{path}"] < 32 * 65536


def test_cms_fetch_timeout_is_per_read_not_whole_request(cms_server: tuple[str, dict[str, int]]) -> None:
    # Documented limit: a slow drip inside the byte limit is not cut off by the timeout.
    url, _ = cms_server
    examples = cms_examples.fetch_cms_examples(api_url=f"{url}/drip", limit=1, timeout_seconds=0.3)
    assert len(examples) == 1


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?limit=99",
        "?type=podcast.EpisodePage&tag=AI&tag=Tech&search=C%2B%2B&empty=",
        "?type=podcast.EpisodePage&limit=99&limit=100",
    ],
)
def test_cms_fetch_preserves_query_and_overrides_url_limit(monkeypatch: pytest.MonkeyPatch, query: str) -> None:
    requests: list[httpx.Request] = []
    item = {"id": 42, "title": "Episode", "description": "<p>Notes</p>"}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps([item, item]).encode()))

    client_class = httpx.Client

    def client(*, follow_redirects: bool, timeout: httpx.Timeout) -> httpx.Client:
        return client_class(follow_redirects=follow_redirects, timeout=timeout, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cms_examples.httpx, "Client", client)
    api_url = f"https://cms.example/api/{query}"
    examples = cms_examples.fetch_cms_examples(api_url=api_url, limit=1)

    assert len(requests) == 1
    expected_filters = [(key, value) for key, value in httpx.URL(api_url).params.multi_items() if key != "limit"]
    actual_filters = [(key, value) for key, value in requests[0].url.params.multi_items() if key != "limit"]
    assert actual_filters == expected_filters
    assert requests[0].url.params.get_list("limit") == ["1"]
    assert len(examples) == 1
    assert examples[0].api_url == api_url
