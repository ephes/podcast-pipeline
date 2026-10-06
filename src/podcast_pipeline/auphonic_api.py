from __future__ import annotations

import base64
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


class AuphonicApiError(RuntimeError):
    pass


class AuphonicProductionFailedError(AuphonicApiError):
    """The polled production reached a terminal state other than Done."""


# Production status codes as returned by https://auphonic.com/api/info/production_status.json.
# Anything not listed as done or terminal (0 File Upload, 1 Waiting, 4 Audio Processing,
# 5 Audio Encoding, 6 Outgoing File Transfer, 7 Mono Mixdown, 8 Split On Chapters,
# 12 Incoming File Transfer, 13 Stopping, 14 Speech Recognition, unknown codes) is running.
_STATUS_BY_CODE: dict[int, str] = {
    2: "error",  # Error
    3: "done",  # Done
    9: "error",  # Incomplete
    10: "not_started",  # Not Started Yet
    11: "error",  # Outdated
    15: "changed",  # Production Changed
    98: "error",  # Empty Production
}
_STATUS_BY_NAME: dict[str, str] = {
    "done": "done",
    "error": "error",
    "incomplete": "error",
    "not started yet": "not_started",
    "production not started yet": "not_started",
    "outdated": "error",
    "production outdated": "error",
    "production changed": "changed",
    "empty production": "error",
}


@dataclass(frozen=True)
class AuphonicCredentials:
    """Either an API key (sent as a Bearer token) or a username and password (HTTP Basic)."""

    base_url: str
    api_key: str | None = None
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True)
class AuphonicOutputFile:
    url: str
    filename: str


@dataclass(frozen=True)
class AuphonicProduction:
    uuid: str
    status: object | None
    status_string: str | None
    output_files: tuple[AuphonicOutputFile, ...]


def load_auphonic_credentials() -> AuphonicCredentials:
    base_url = os.environ.get("AUPHONIC_BASE_URL", "https://auphonic.com/api")
    api_key = (os.environ.get("AUPHONIC_API_KEY") or "").strip()
    if api_key:
        return AuphonicCredentials(base_url=base_url, api_key=api_key)
    username = (os.environ.get("AUPHONIC_USER") or os.environ.get("AUPHONIC_USERNAME") or "").strip()
    password = os.environ.get("AUPHONIC_PASSWORD") or ""
    if username and password:
        return AuphonicCredentials(base_url=base_url, username=username, password=password)
    raise AuphonicApiError(
        "Missing Auphonic credentials. Set AUPHONIC_API_KEY (an Auphonic API key), "
        "or AUPHONIC_USER and AUPHONIC_PASSWORD."
    )


class _ApiOriginAuthTransport(httpx.BaseTransport):
    """Attach Auphonic credentials only to requests for the API's own origin.

    It wraps the real transport, so the check runs on every request that goes
    out, including each redirect hop. Output download URLs and redirects may
    point at other origins; those never receive the API key or password.
    """

    def __init__(self, credentials: AuphonicCredentials, wrapped: httpx.BaseTransport) -> None:
        if credentials.api_key:
            self._header = f"Bearer {credentials.api_key}"
        elif credentials.username and credentials.password:
            token = base64.b64encode(f"{credentials.username}:{credentials.password}".encode()).decode("ascii")
            self._header = f"Basic {token}"
        else:
            raise AuphonicApiError("Auphonic credentials need an API key or a username and password.")
        self._origin = _origin(httpx.URL(credentials.base_url))
        self._wrapped = wrapped

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if _origin(request.url) == self._origin:
            request.headers["Authorization"] = self._header
        else:
            request.headers.pop("Authorization", None)
        return self._wrapped.handle_request(request)

    def close(self) -> None:
        self._wrapped.close()


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    port = url.port
    if port is None:
        port = {"http": 80, "https": 443}.get(url.scheme)
    return (url.scheme, url.host, port)


class AuphonicClient:
    def __init__(
        self,
        credentials: AuphonicCredentials,
        *,
        timeout_seconds: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = credentials.base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            transport=_ApiOriginAuthTransport(credentials, transport or httpx.HTTPTransport()),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AuphonicClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def start_production(
        self,
        payload: Mapping[str, Any],
        *,
        on_created: Callable[[str], None] | None = None,
    ) -> AuphonicProduction:
        """Create an Auphonic production from ``payload`` and start it.

        Follows the JSON API flow: create the production (which only saves it),
        upload a local input file to ``production/{uuid}/upload.json`` (URL inputs
        are part of the create request), then call ``production/{uuid}/start.json``.
        ``on_created`` receives the UUID before the paid start request, so a
        caller can persist it even when the start's outcome is unknown.
        """
        input_files = _extract_input_files(payload)
        if len(input_files) > 1:
            raise AuphonicApiError(
                "Auphonic single-track productions take exactly one input file "
                f"(got {len(input_files)}); multitrack productions are not supported."
            )

        body = _strip_input_files(payload)
        local_path: Path | None = None
        if input_files:
            if _looks_like_url(input_files[0]):
                body["input_file"] = input_files[0]
            else:
                local_path = Path(input_files[0])
                if not local_path.is_file():
                    raise AuphonicApiError(f"Auphonic input file not found: {local_path}")

        created = _parse_production(
            _request_json(self._client, "POST", f"{self._base_url}/productions.json", json=body)
        )
        if on_created is not None:
            on_created(created.uuid)
        production_url = f"{self._base_url}/production/{created.uuid}"
        try:
            if local_path is not None:
                with local_path.open("rb") as handle:
                    _request_json(
                        self._client,
                        "POST",
                        f"{production_url}/upload.json",
                        files={"input_file": (local_path.name, handle, "application/octet-stream")},
                    )
            return _parse_production(_request_json(self._client, "POST", f"{production_url}/start.json"))
        except (AuphonicApiError, OSError) as exc:
            raise AuphonicApiError(
                f"Auphonic production {created.uuid} was created but starting it failed: {exc}. "
                "A plain rerun follows it if Auphonic did start it; otherwise rerun with "
                "`podcast produce --restart`."
            ) from exc

    def fetch_production(self, uuid: str) -> AuphonicProduction:
        url = f"{self._base_url}/production/{uuid}.json"
        response = _request_json(self._client, "GET", url)
        return _parse_production(response)

    def wait_for_production(
        self,
        uuid: str,
        *,
        poll_interval: float,
        timeout_seconds: float,
        not_started_grace_seconds: float = 0.0,
    ) -> AuphonicProduction:
        """Poll until the production is done; raise on a failure status or timeout.

        ``not_started_grace_seconds`` keeps polling while a production that was
        just started still reports ``Not Started Yet``.
        """
        start = time.monotonic()
        while True:
            production = self.fetch_production(uuid)
            status = _classify_status(production.status, production.status_string)
            if status == "done":
                return production
            if status == "not_started" and time.monotonic() - start <= not_started_grace_seconds:
                status = "running"
            if status != "running":
                raise AuphonicProductionFailedError(_terminal_message(uuid, status, production))
            if time.monotonic() - start > timeout_seconds:
                raise AuphonicApiError(f"Auphonic production {uuid} timed out after {timeout_seconds} seconds.")
            time.sleep(poll_interval)

    def list_output_files(self, uuid: str) -> tuple[AuphonicOutputFile, ...]:
        url = f"{self._base_url}/production/{uuid}/output_files.json"
        response = _request_json(self._client, "GET", url)
        data = _extract_data(response)
        outputs = _parse_output_files_raw(data)
        if outputs:
            return outputs
        if isinstance(data, Mapping) and "output_files" in data:
            return _parse_output_files_raw(data.get("output_files"))
        return ()

    def download_outputs(self, outputs: Sequence[AuphonicOutputFile], output_dir: Path) -> tuple[Path, ...]:
        output_dir.mkdir(parents=True, exist_ok=True)
        used: set[str] = set()
        downloaded: list[Path] = []
        for idx, output in enumerate(outputs, start=1):
            filename = _unique_filename(output.filename, used, idx)
            dest = output_dir / filename
            _download_file(self._client, output.url, dest)
            downloaded.append(dest)
        return tuple(downloaded)


def _request_json(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = client.request(method, url, **kwargs)
    except httpx.RequestError as exc:
        raise AuphonicApiError(f"Auphonic API request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise AuphonicApiError(f"Auphonic API returned invalid JSON (status {response.status_code}).") from exc

    if isinstance(payload, Mapping):
        status = payload.get("status")
        if isinstance(status, str) and status.lower() == "error":
            message = _extract_error_message(payload) or "Auphonic API returned an error."
            raise AuphonicApiError(message)

    if response.status_code >= 400:
        message = _extract_error_message(payload) or f"HTTP {response.status_code}"
        raise AuphonicApiError(f"Auphonic API error ({response.status_code}): {message}")
    if isinstance(payload, Mapping):
        return dict(payload)
    return {"data": payload}


def _extract_error_message(payload: object) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("error_message", "error", "message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    form_errors = payload.get("form_errors")
    if isinstance(form_errors, Mapping) and form_errors:
        return "; ".join(f"{key}: {value}" for key, value in form_errors.items())
    errors = payload.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes, bytearray)):
        parts = [str(item).strip() for item in errors if str(item).strip()]
        if parts:
            return "; ".join(parts)
    data = payload.get("data")
    if isinstance(data, Mapping):
        for key in ("error", "message", "detail"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _extract_data(payload: Mapping[str, Any]) -> object:
    if "data" in payload:
        return payload["data"]
    return payload


def _parse_production(payload: Mapping[str, Any]) -> AuphonicProduction:
    data = _extract_data(payload)
    if not isinstance(data, Mapping):
        raise AuphonicApiError("Auphonic API response missing production data.")
    uuid = _required_str(data.get("uuid"), key="uuid")
    status = data.get("status")
    status_string = _optional_str(
        data.get("status_string") or data.get("status_text") or data.get("status_label"),
    )
    output_files = _parse_output_files_raw(data.get("output_files"))
    return AuphonicProduction(
        uuid=uuid,
        status=status,
        status_string=status_string,
        output_files=output_files,
    )


def _parse_output_files_raw(raw: object) -> tuple[AuphonicOutputFile, ...]:
    if raw is None:
        return ()
    if isinstance(raw, Mapping) and "output_files" in raw:
        raw = raw.get("output_files")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    outputs: list[AuphonicOutputFile] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            continue
        url = _optional_str(item.get("download_url") or item.get("url") or item.get("link"))
        if not url:
            continue
        filename = _optional_str(
            item.get("filename") or item.get("file_name") or item.get("basename"),
        )
        if not filename:
            filename = _filename_from_url(url) or f"output_{idx}"
        outputs.append(AuphonicOutputFile(url=url, filename=filename))
    return tuple(outputs)


def _extract_input_files(payload: Mapping[str, Any]) -> tuple[str, ...]:
    if "input_file" in payload:
        value = payload.get("input_file")
        if isinstance(value, str) and value.strip():
            return (value.strip(),)
        return ()
    raw = payload.get("input_files")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        items = [str(item).strip() for item in raw if str(item).strip()]
        return tuple(items)
    return ()


def _strip_input_files(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in {"input_file", "input_files"}}


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


def _required_str(value: object, *, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuphonicApiError(f"Auphonic API missing {key}.")
    return value.strip()


def _optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _status_code(status: object) -> int | None:
    if isinstance(status, bool):
        return None
    if isinstance(status, int):
        return status
    if isinstance(status, str) and status.strip().isdigit():
        return int(status.strip())
    return None


def _classify_status(status: object, status_string: str | None) -> str:
    """Map an Auphonic production status to ``done``, ``running`` or a terminal problem.

    The numeric code is authoritative; ``status_string`` is only consulted when
    the code is missing. Unknown codes count as running, bounded by the wait
    timeout.
    """
    code = _status_code(status)
    if code is not None:
        return _STATUS_BY_CODE.get(code, "running")
    text = status_string
    if text is None and isinstance(status, str):
        text = status
    if text is not None:
        normalized = text.strip().lower()
        for name, classification in _STATUS_BY_NAME.items():
            if normalized == name:
                return classification
    return "running"


def _terminal_message(uuid: str, classification: str, production: AuphonicProduction) -> str:
    detail = production.status_string or str(production.status if production.status is not None else "unknown")
    hint = "Fix the cause, then rerun with `podcast produce --restart` to start a new production."
    if classification == "not_started":
        return f"Auphonic production {uuid} was never started (status: {detail}). {hint}"
    if classification == "changed":
        return (
            f"Auphonic production {uuid} was changed after it finished (status: {detail}); "
            f"reprocess it in Auphonic, or {hint[0].lower()}{hint[1:]}"
        )
    return f"Auphonic production {uuid} failed (status: {detail}). {hint}"


def _filename_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    return name or None


def _unique_filename(filename: str, used: set[str], idx: int) -> str:
    name = Path(filename).name
    if not name:
        name = f"output_{idx}"
    if name not in used:
        used.add(name)
        return name
    stem = Path(name).stem
    suffix = Path(name).suffix
    counter = 2
    while True:
        candidate = f"{stem}_{counter}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        counter += 1


def _download_file(client: httpx.Client, url: str, dest: Path) -> None:
    with client.stream("GET", url) as response:
        if response.status_code >= 400:
            raise AuphonicApiError(f"Failed to download {url} (status {response.status_code}).")
        _atomic_write_stream(dest, response.iter_bytes())


def _atomic_write_stream(path: Path, chunks: Iterable[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as tmp:
        for chunk in chunks:
            if not chunk:
                continue
            tmp.write(chunk)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    try:
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
