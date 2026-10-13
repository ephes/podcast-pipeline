from __future__ import annotations

import json
import math
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpcore
import httpx

from podcast_pipeline.rss_examples import normalize_html, normalize_text
from podcast_pipeline.workspace_store import atomic_write_text


class CmsExamplesError(RuntimeError):
    pass


@dataclass(frozen=True)
class CmsFieldMapping:
    title: str = "title"
    summary: str | None = "summary"
    description: str | None = "description"
    shownotes: str | None = "shownotes"
    tags: str | None = "tags"
    link: str | None = "url"
    slug: str | None = "slug"
    published: str | None = "first_published_at"
    page_id: str | None = "id"


@dataclass(frozen=True)
class CmsEpisodeExample:
    title: str
    description_html: str | None
    shownotes_html: str | None
    tags: tuple[str, ...]
    summary: str | None
    link: str | None
    slug: str | None
    published: str | None
    page_id: str | None
    api_url: str

    def example_id(self) -> str:
        url = httpx.URL(self.api_url)
        source = str(
            url.copy_with(
                username="",
                password="",
                query=None,
                fragment=None,
                raw_path=url.raw_path.split(b"?", 1)[0].rstrip(b"/"),
            )
        )
        seed = json.dumps([source, self.page_id or self.link or self.title], ensure_ascii=False)
        digest = sha256(seed.encode()).hexdigest()[:12]
        return f"cms_{digest}"

    def build_input_text(self) -> str:
        parts = [f"Title: {self.title}"]
        if self.summary:
            parts.append(f"Summary: {self.summary}")
        return "\n".join(parts)

    def build_output_text(self) -> str:
        if self.description_html:
            return self.description_html
        if self.shownotes_html:
            return self.shownotes_html
        return ""

    def to_record(self) -> dict[str, Any]:
        return {
            "version": 1,
            "source": "cms",
            "example_id": self.example_id(),
            "input": self.build_input_text(),
            "output": self.build_output_text(),
            "title": self.title,
            "summary": self.summary,
            "link": self.link,
            "slug": self.slug,
            "published": self.published,
            "tags": list(self.tags),
            "description_html": self.description_html,
            "shownotes_html": self.shownotes_html,
            "api_url": self.api_url,
            "page_id": self.page_id,
        }


_TAG_SPLIT_RE = re.compile(r"[;,]")
_MAX_CMS_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_CMS_REDIRECTS = 5
_CMS_FETCH_THREAD_NAME = "cms-examples-fetch"
_CMS_TIMEOUT_MESSAGE = "CMS API request exceeded its timeout."


def fetch_cms_examples(
    *,
    api_url: str,
    limit: int,
    timeout_seconds: float = 20.0,
    fields: CmsFieldMapping | None = None,
) -> list[CmsEpisodeExample]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    payload = _fetch_cms_json(api_url=api_url, timeout_seconds=timeout_seconds, limit=limit)
    return parse_cms_examples(payload, api_url=api_url, limit=limit, fields=fields)


def parse_cms_examples(
    payload: Any,
    *,
    api_url: str,
    limit: int,
    fields: CmsFieldMapping | None = None,
) -> list[CmsEpisodeExample]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    mapping = fields or CmsFieldMapping()
    items = _extract_items(payload)

    examples: list[CmsEpisodeExample] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        example = _parse_item(item, api_url=api_url, fields=mapping)
        if example is None:
            continue
        if not example.build_output_text():
            continue
        examples.append(example)
        if len(examples) >= limit:
            break

    if not examples:
        raise CmsExamplesError("CMS API returned no usable episodes.")
    return examples


def write_cms_examples_jsonl(
    *,
    examples: Sequence[CmsEpisodeExample],
    output_path: Path,
) -> None:
    output_path = output_path.expanduser()
    content = "".join(
        json.dumps(example.to_record(), ensure_ascii=False, sort_keys=True) + "\n" for example in examples
    )
    atomic_write_text(output_path, content)


def _fetch_cms_json(*, api_url: str, timeout_seconds: float, limit: int) -> Any:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise CmsExamplesError("CMS timeout must be finite and > 0.")
    body = _read_cms_response_within_deadline(api_url=api_url, timeout_seconds=timeout_seconds, limit=limit)
    try:
        return json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise CmsExamplesError("CMS API returned invalid JSON.") from exc


class _DeadlineStream(httpcore.NetworkStream):
    """Network stream whose every read, write and TLS handshake ends at one deadline."""

    def __init__(self, stream: httpcore.NetworkStream, deadline: float, release: Callable[[], None]) -> None:
        self._stream = stream
        self._deadline = deadline
        self._release = release

    def _timeout(self, timeout: float | None, error: type[Exception]) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise error(_CMS_TIMEOUT_MESSAGE)
        return remaining if timeout is None else min(timeout, remaining)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(max_bytes, self._timeout(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, self._timeout(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self._release()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        stream = self._stream.start_tls(ssl_context, server_hostname, self._timeout(timeout, httpcore.ConnectTimeout))
        return _DeadlineStream(stream, self._deadline, self._release)

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    """
    Network backend that keeps every connection of one fetch inside its deadline.

    Reads and writes get at most the remaining time, so header or body bytes that
    keep arriving cannot reset the timeout. ``abort()`` shuts down the connections
    that are still open (also one inside a TLS handshake) and refuses later ones,
    so a fetch that was given up does not keep a socket.
    """

    def __init__(self, backend: httpcore.NetworkBackend, deadline: float) -> None:
        self._backend = backend
        self._deadline = deadline
        self._lock = threading.Lock()
        self._aborted = False
        # Duplicates of the connected sockets: wrapping a socket for TLS detaches
        # the original object, a duplicate still reaches the connection.
        self._sockets: list[socket.socket] = []

    def _track(self, stream: httpcore.NetworkStream) -> httpcore.NetworkStream:
        try:
            sock = stream.get_extra_info("socket")
            if not isinstance(sock, socket.socket):
                raise httpcore.ConnectError("CMS fetch connection cannot be bounded.")
            with self._lock:
                if self._aborted or time.monotonic() >= self._deadline:
                    raise httpcore.ConnectTimeout(_CMS_TIMEOUT_MESSAGE)
                duplicate = sock.dup()
                self._sockets.append(duplicate)
        except BaseException:
            stream.close()
            raise
        return _DeadlineStream(stream, self._deadline, lambda: self._release(duplicate))

    def _release(self, duplicate: socket.socket) -> None:
        with self._lock:
            if duplicate not in self._sockets:
                return
            self._sockets.remove(duplicate)
        duplicate.close()

    def _connect_timeout(self, timeout: float | None) -> float:
        remaining = self._deadline - time.monotonic()
        if self._aborted or remaining <= 0:
            raise httpcore.ConnectTimeout(_CMS_TIMEOUT_MESSAGE)
        return remaining if timeout is None else min(timeout, remaining)

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        return self._track(
            self._backend.connect_tcp(
                host,
                port,
                timeout=self._connect_timeout(timeout),
                local_address=local_address,
                socket_options=socket_options,
            )
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        return self._track(
            self._backend.connect_unix_socket(
                path, timeout=self._connect_timeout(timeout), socket_options=socket_options
            )
        )

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


def _bound_client_connections(client: httpx.Client, deadline: float) -> list[_DeadlineBackend]:
    # httpx has no public way to choose the network backend of its default and
    # proxy transports. Fail closed when their layout is not the expected one.
    backends = []
    for transport in {id(t): t for t in (client._transport, *client._mounts.values()) if t is not None}.values():
        pool = getattr(transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if pool is None or not isinstance(backend, httpcore.NetworkBackend):
            raise CmsExamplesError("CMS fetch cannot bound this HTTP transport.")
        bounded = _DeadlineBackend(backend, deadline)
        pool._network_backend = bounded
        backends.append(bounded)
    return backends


def _read_cms_response_within_deadline(*, api_url: str, timeout_seconds: float, limit: int) -> bytes:
    # One deadline includes name resolution, connection, redirects, headers and body;
    # per-operation timeouts alone allow an endlessly dripping response and do not
    # cover a blocking DNS lookup. The request runs in a daemon thread, so neither
    # this call nor interpreter exit waits for a lookup that is still in flight.
    deadline = time.monotonic() + timeout_seconds
    done = threading.Event()
    outcome: list[bytes | BaseException] = []
    client = httpx.Client(follow_redirects=False)
    try:
        backends = _bound_client_connections(client, deadline)
    except BaseException:
        client.close()
        raise

    def abort() -> None:
        for backend in backends:
            backend.abort()

    def read() -> None:
        try:
            with client:
                outcome.append(_read_cms_response(client, api_url=api_url, deadline=deadline, limit=limit))
        except BaseException as exc:  # noqa: BLE001 - re-raised in the calling thread
            outcome.append(exc)
        finally:
            abort()
            done.set()

    threading.Thread(target=read, name=_CMS_FETCH_THREAD_NAME, daemon=True).start()
    if not done.wait(timeout_seconds):
        # Give up the worker: its open connections are shut down and it gets no new
        # one, so it ends as soon as the operation it is blocked in returns.
        abort()
        raise CmsExamplesError(_CMS_TIMEOUT_MESSAGE)
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CmsExamplesError(_CMS_TIMEOUT_MESSAGE)
    return remaining


def _read_cms_response(client: httpx.Client, *, api_url: str, deadline: float, limit: int) -> bytes:
    try:
        # Redirects are followed by hand: automatic redirects read (and decompress)
        # every intermediate response body before the byte budget can be checked.
        request = client.build_request(
            "GET", api_url, params={"limit": limit}, headers={"Accept-Encoding": "identity"}
        )
        for _ in range(_MAX_CMS_REDIRECTS + 1):
            request.extensions = {
                **request.extensions,
                "timeout": httpx.Timeout(_remaining_seconds(deadline)).as_dict(),
            }
            response = client.send(request, stream=True)
            try:
                if response.next_request is not None:
                    request = response.next_request
                    continue
                if response.status_code >= 400:
                    raise CmsExamplesError(f"CMS API request failed with HTTP {response.status_code}.")
                # Refuse unsolicited compression instead of expanding an unbounded
                # compressed chunk before the byte budget can be checked.
                if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                    raise CmsExamplesError("CMS API returned unsupported Content-Encoding; request uncompressed JSON.")
                body = bytearray()
                # No chunk_size: chunks arrive as they are received, so a drip of tiny
                # chunks reaches the deadline check instead of filling a buffer first.
                for chunk in response.iter_raw():
                    _remaining_seconds(deadline)
                    if len(body) + len(chunk) > _MAX_CMS_RESPONSE_BYTES:
                        raise CmsExamplesError("CMS API response exceeds the 8 MiB byte limit.")
                    body.extend(chunk)
                return bytes(body)
            finally:
                response.close()
        raise CmsExamplesError(f"CMS API redirected more than {_MAX_CMS_REDIRECTS} times.")
    except httpx.TimeoutException as exc:
        raise CmsExamplesError(_CMS_TIMEOUT_MESSAGE) from exc
    except httpx.RequestError as exc:
        raise CmsExamplesError("Failed to fetch CMS API.") from exc


def _extract_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("items", "results", "pages"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise CmsExamplesError("CMS API response missing items list.")


def _parse_item(
    item: Mapping[str, Any],
    *,
    api_url: str,
    fields: CmsFieldMapping,
) -> CmsEpisodeExample | None:
    title = _coerce_text(_extract_field(item, fields.title))
    if not title:
        return None

    description = _coerce_html(_extract_field(item, fields.description))
    shownotes = _coerce_html(_extract_field(item, fields.shownotes))
    tags = _coerce_tags(_extract_field(item, fields.tags))
    summary = _coerce_text(_extract_field(item, fields.summary))
    link = _coerce_text(_extract_field(item, fields.link))
    if link is None:
        link = _coerce_text(
            _extract_meta(item, "html_url") or _extract_meta(item, "url") or _extract_meta(item, "detail_url")
        )
    slug = _coerce_text(_extract_field(item, fields.slug))
    if slug is None:
        slug = _coerce_text(_extract_meta(item, "slug"))
    published = _coerce_text(_extract_field(item, fields.published))
    if published is None:
        published = _coerce_text(_extract_meta(item, "first_published_at") or _extract_meta(item, "published_at"))
    page_id = _coerce_id(_extract_field(item, fields.page_id))

    if not description and not shownotes:
        return None

    return CmsEpisodeExample(
        title=title,
        description_html=description,
        shownotes_html=shownotes,
        tags=tags,
        summary=summary,
        link=link,
        slug=slug,
        published=published,
        page_id=page_id,
        api_url=api_url,
    )


def _extract_field(item: Mapping[str, Any], field: str | None) -> Any:
    if not field:
        return None
    current: Any = item
    for part in field.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _extract_meta(item: Mapping[str, Any], field: str) -> Any:
    meta = item.get("meta")
    if not isinstance(meta, Mapping):
        return None
    return meta.get(field)


def _coerce_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = normalize_text(value)
        return text or None
    if isinstance(value, Mapping):
        nested = value.get("value") or value.get("text") or value.get("html")
        return _coerce_text(nested)
    return normalize_text(str(value)) or None


def _coerce_html(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        html_value = normalize_html(value)
        return html_value or None
    if isinstance(value, Mapping):
        nested = value.get("html") or value.get("value")
        return _coerce_html(nested)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        parts = [_coerce_html(item) for item in value]
        combined = "\n\n".join(part for part in parts if part)
        return normalize_html(combined) if combined else None
    return normalize_html(str(value)) or None


def _coerce_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip() or None


def _coerce_tags(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = _TAG_SPLIT_RE.split(value)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = [_coerce_tag(item) for item in value]
    elif isinstance(value, Mapping):
        items = [_coerce_tag(value)]
    else:
        items = [str(value)]

    cleaned = []
    for item in items:
        if not item:
            continue
        text = normalize_text(item)
        if text:
            cleaned.append(text)

    seen: set[str] = set()
    deduped: list[str] = []
    for item in cleaned:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return tuple(deduped)


def _coerce_tag(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("name", "title", "slug"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested
    return str(value)
