from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlparse

import httpx


class AuphonicApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuphonicCredentials:
    username: str
    api_key: str
    base_url: str


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
    username = os.environ.get("AUPHONIC_USER") or os.environ.get("AUPHONIC_USERNAME")
    api_key = os.environ.get("AUPHONIC_API_KEY") or os.environ.get("AUPHONIC_PASSWORD")
    if not username or not api_key:
        raise AuphonicApiError("Missing Auphonic credentials. Set AUPHONIC_USER and AUPHONIC_API_KEY.")
    base_url = os.environ.get("AUPHONIC_BASE_URL", "https://auphonic.com/api")
    return AuphonicCredentials(username=username, api_key=api_key, base_url=base_url)


class AuphonicClient:
    def __init__(self, credentials: AuphonicCredentials, *, timeout_seconds: float = 300.0) -> None:
        self._base_url = credentials.base_url.rstrip("/")
        self._client = httpx.Client(
            auth=(credentials.username, credentials.api_key),
            timeout=timeout_seconds,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AuphonicClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def start_production(self, payload: Mapping[str, Any]) -> AuphonicProduction:
        input_files = _extract_input_files(payload)
        payload_core = _strip_input_files(payload)
        url = f"{self._base_url}/productions.json"

        if not input_files:
            response = _request_json(self._client, "POST", url, json=payload_core)
            return _parse_production(response)

        if _all_urls(input_files):
            payload_core = dict(payload_core)
            if len(input_files) == 1:
                payload_core["input_file"] = input_files[0]
            else:
                payload_core["input_files"] = list(input_files)
            response = _request_json(self._client, "POST", url, json=payload_core)
            return _parse_production(response)

        if _any_urls(input_files):
            raise AuphonicApiError("Auphonic input files cannot mix URLs with local paths.")

        data = _payload_to_form(payload_core)
        field = "input_file"
        files: list[tuple[str, tuple[str, IO[bytes], str]]] = []
        for item in input_files:
            path = Path(item)
            if not path.exists() or not path.is_file():
                raise AuphonicApiError(f"Auphonic input file not found: {path}")
            handle: IO[bytes] = path.open("rb")
            files.append((field, (path.name, handle, "application/octet-stream")))

        try:
            response = _request_json(self._client, "POST", url, data=data, files=files)
        finally:
            for _, (_, handle, _) in files:
                try:
                    handle.close()
                except OSError:
                    pass
        return _parse_production(response)

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
    ) -> AuphonicProduction:
        start = time.monotonic()
        while True:
            production = self.fetch_production(uuid)
            status = _classify_status(production.status, production.status_string)
            if status == "done":
                return production
            if status == "error":
                detail = production.status_string or str(production.status or "unknown")
                raise AuphonicApiError(f"Auphonic production {uuid} failed (status: {detail}).")
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
    for key in ("error", "message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
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


def _payload_to_form(payload: Mapping[str, Any]) -> dict[str, str]:
    data: dict[str, str] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple)):
            data[key] = json.dumps(value)
        else:
            data[key] = str(value)
    return data


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


def _any_urls(values: Sequence[str]) -> bool:
    return any(_looks_like_url(item) for item in values)


def _all_urls(values: Sequence[str]) -> bool:
    return all(_looks_like_url(item) for item in values)


def _required_str(value: object, *, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuphonicApiError(f"Auphonic API missing {key}.")
    return value.strip()


def _optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _classify_status(status: object, status_string: str | None) -> str:
    text = status_string
    if text is None and isinstance(status, str):
        text = status
    if text is not None:
        normalized = text.strip().lower()
        if any(token in normalized for token in ("done", "complete", "completed", "finished")):
            return "done"
        if any(token in normalized for token in ("error", "failed", "aborted")):
            return "error"
    if isinstance(status, int):
        if status >= 4:
            return "error"
        if status == 3:
            return "done"
    return "running"


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
