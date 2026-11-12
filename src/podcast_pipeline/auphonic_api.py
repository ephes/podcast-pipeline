from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterable, Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx


class AuphonicApiError(RuntimeError):
    pass


class _OutputRollbackError(AuphonicApiError):
    """Publication rollback failed; its staging directory must be retained."""


class AuphonicProductionFailedError(AuphonicApiError):
    """The polled production reached a terminal state other than Done."""


class AuphonicNotFoundError(AuphonicApiError):
    """The Auphonic API answered 404, for example for a production that no longer exists."""


# Auphonic production UUIDs are URL-safe; anything else never reaches a request URL.
_PRODUCTION_UUID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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
class AuphonicTrack:
    id: str
    type: str | None
    input_file: str | None


@dataclass(frozen=True)
class AuphonicProduction:
    uuid: str
    status: object | None
    status_string: str | None
    output_files: tuple[AuphonicOutputFile, ...]
    is_multitrack: bool | None = None
    tracks: tuple[AuphonicTrack, ...] = ()
    input_file: str | None = None
    change_allowed: bool | None = None
    chapter_titles: tuple[str, ...] = ()
    output_formats: tuple[str, ...] = ()


# Payload keys that carry a production's audio inputs; an update never sends them.
_INPUT_KEYS = frozenset({"is_multitrack", "multi_input_files", "input_file", "input_files"})


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


class _ApiOriginCredentials:
    """Attach Auphonic credentials only to requests for the API's own origin.

    Installed as an httpx request event hook, which runs before every request
    that goes out, including each redirect hop. Output download URLs and
    redirects may point at other origins; those never receive the API key or
    password. Using a hook (not a custom transport) keeps httpx's environment
    proxy support (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY).
    """

    def __init__(self, credentials: AuphonicCredentials) -> None:
        if credentials.api_key:
            self._header = f"Bearer {credentials.api_key}"
        elif credentials.username and credentials.password:
            token = base64.b64encode(f"{credentials.username}:{credentials.password}".encode()).decode("ascii")
            self._header = f"Basic {token}"
        else:
            raise AuphonicApiError("Auphonic credentials need an API key or a username and password.")
        self._origin = _origin(httpx.URL(credentials.base_url))

    def __call__(self, request: httpx.Request) -> None:
        if _origin(request.url) == self._origin:
            request.headers["Authorization"] = self._header
        else:
            request.headers.pop("Authorization", None)


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
        download_max_bytes: int | None = None,
        download_timeout_seconds: float | None = None,
    ) -> None:
        if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
            raise AuphonicApiError("Auphonic transport must support both sync and async requests")
        try:
            byte_limit = (
                int(os.environ.get("PODCAST_PIPELINE_DOWNLOAD_MAX_BYTES", str(4 * 1024**3)))
                if download_max_bytes is None
                else download_max_bytes
            )
            download_timeout = (
                float(os.environ.get("PODCAST_PIPELINE_DOWNLOAD_TIMEOUT_SECONDS", "3600"))
                if download_timeout_seconds is None
                else download_timeout_seconds
            )
        except ValueError as exc:
            raise AuphonicApiError("Invalid Auphonic download byte/time budget") from exc
        if isinstance(byte_limit, bool) or not isinstance(byte_limit, int) or byte_limit <= 0:
            raise AuphonicApiError("download_max_bytes must be a positive integer")
        _positive_finite("download_timeout_seconds", download_timeout)
        self._download_max_bytes = byte_limit
        self._download_timeout_seconds = download_timeout
        self._timeout_seconds = timeout_seconds
        self._credentials = credentials
        self._transport = transport
        self._base_url = credentials.base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            transport=transport,
            event_hooks={"request": [_ApiOriginCredentials(credentials)]},
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

        Follows the JSON API flow: check that the preset matches the production
        type, create the production (which only saves it), upload local inputs
        to ``production/{uuid}/upload.json`` (URL inputs are part of the create
        request), then call ``production/{uuid}/start.json``. A multitrack
        payload (``is_multitrack``) uploads every local stem in one multipart
        request, with the track id as the form field name. ``on_created``
        receives the UUID before the paid start request, so a caller can persist
        it even when the start's outcome is unknown.
        """
        multitrack = payload.get("is_multitrack") is True
        if multitrack:
            body, uploads = _prepare_multitrack(payload)
        else:
            body, uploads = _prepare_single_track(payload)

        preset = payload.get("preset")
        if isinstance(preset, str) and preset.strip():
            self._check_preset(preset.strip(), multitrack=multitrack, track_ids=_track_ids(body))

        created = _parse_production(
            _request_json(self._client, "POST", f"{self._base_url}/productions.json", json=body)
        )
        if on_created is not None:
            on_created(created.uuid)
        production_url = f"{self._base_url}/production/{created.uuid}"
        try:
            if uploads:
                with ExitStack() as stack:
                    files = [
                        (field, (path.name, stack.enter_context(path.open("rb")), "application/octet-stream"))
                        for field, path in uploads
                    ]
                    _request_json(self._client, "POST", f"{production_url}/upload.json", files=files)
            return _parse_production(_request_json(self._client, "POST", f"{production_url}/start.json"))
        except (AuphonicApiError, OSError) as exc:
            raise AuphonicApiError(
                f"Auphonic production {created.uuid} was created but starting it failed: {exc}. "
                "A plain rerun follows it if Auphonic did start it; otherwise rerun with "
                "`podcast produce --restart`."
            ) from exc

    def _check_preset(self, preset: str, *, multitrack: bool, track_ids: Sequence[str]) -> None:
        """Fail before creating a production the preset would break.

        Auphonic multitrack presets (``is_multitrack``) only apply to multitrack
        productions and single-track presets only to single-track ones. Auphonic
        also appends a preset's own multitrack tracks to the production's, so a
        preset that carries speaker tracks yields duplicate or extra tracks.
        """
        data = _extract_data(_request_json(self._client, "GET", f"{self._base_url}/preset/{preset}.json"))
        if not isinstance(data, Mapping):
            return
        preset_multitrack = data.get("is_multitrack")
        if isinstance(preset_multitrack, bool) and preset_multitrack != multitrack:
            if multitrack:
                raise AuphonicApiError(
                    f"Auphonic preset {preset} is a single-track preset, but the production is multitrack. "
                    "Create a multitrack preset in Auphonic (Multitrack mode) and set auphonic.preset to it."
                )
            raise AuphonicApiError(
                f"Auphonic preset {preset} is a multitrack preset, but auphonic.mode is single_track. "
                "Use a single-track preset, or drop auphonic.mode to produce the speaker stems as multitrack."
            )
        if not multitrack:
            return
        preset_tracks = _preset_speaker_track_ids(data.get("multi_input_files"))
        if not preset_tracks:
            return
        collisions = [track_id for track_id in preset_tracks if track_id in track_ids]
        detail = f" (colliding with auphonic.tracks: {', '.join(collisions)})" if collisions else ""
        raise AuphonicApiError(
            f"Auphonic preset {preset} contains multitrack tracks {', '.join(preset_tracks)}{detail}. "
            "Auphonic adds a preset's tracks to the production's, which duplicates or adds speaker tracks. "
            "Remove all tracks from the preset; per-track algorithms belong in auphonic.tracks[].algorithms."
        )

    def rerun_production(
        self,
        stored: AuphonicProduction,
        payload: Mapping[str, Any],
        *,
        before_update: Callable[[], None] | None = None,
    ) -> AuphonicProduction:
        """Update the existing production ``stored`` from ``payload`` and start it again.

        Nothing is uploaded: the production keeps its input files. Everything
        else in ``payload`` (preset, title, output basename, metadata, chapters)
        goes into one update request. Sending the preset makes Auphonic re-apply
        it, which replaces the output files with the preset's and clears the
        chapters before the request's own chapters are added. Per-track
        ``offset`` and ``algorithms`` go to each track's own endpoint, because
        ``multi_input_files`` in a production update would add duplicate tracks
        and ``reset_data`` would delete the uploaded stems. The update is
        checked before the paid start request. ``before_update`` runs after
        the local checks, right before the first request that changes the
        production.
        """
        body, track_updates = update_requests(payload)
        multitrack = payload.get("is_multitrack") is True
        _check_rerunnable(stored, payload, multitrack=multitrack)
        preset = payload.get("preset")
        if isinstance(preset, str) and preset.strip():
            self._check_preset(
                preset.strip(),
                multitrack=multitrack,
                track_ids=[str(track["id"]) for track in track_updates],
            )

        if before_update is not None:
            before_update()
        production_url = f"{self._base_url}/production/{stored.uuid}"
        updated = _parse_production(_request_json(self._client, "POST", f"{production_url}.json", json=body))
        for track in track_updates:
            track_url = f"{production_url}/multi_input_files/{quote(str(track['id']), safe='')}.json"
            _request_json(self._client, "POST", track_url, json=track)
        if track_updates:
            updated = self.fetch_production(stored.uuid)
        _check_updated(updated, payload)
        try:
            return _parse_production(_request_json(self._client, "POST", f"{production_url}/start.json"))
        except AuphonicApiError as exc:
            raise AuphonicApiError(
                f"Auphonic production {stored.uuid} was updated but starting it failed: {exc}. "
                "A plain rerun follows it if Auphonic did start it; otherwise rerun with "
                "`podcast produce --update`."
            ) from exc

    def fetch_production(self, uuid: str, *, timeout_seconds: float | None = None) -> AuphonicProduction:
        url = f"{self._base_url}/production/{uuid}.json"
        if timeout_seconds is None:
            response = _request_json(self._client, "GET", url)
        else:
            response = asyncio.run(self._fetch_with_deadline(url, timeout_seconds))
        return _parse_production(response)

    async def _fetch_with_deadline(self, url: str, seconds: float) -> dict[str, Any]:
        assert self._transport is None or isinstance(self._transport, httpx.AsyncBaseTransport)
        try:
            async with asyncio.timeout(seconds):
                async with httpx.AsyncClient(
                    timeout=min(self._timeout_seconds, seconds),
                    follow_redirects=True,
                    transport=self._transport,
                    event_hooks={"request": [self._authorize_async]},
                ) as client:
                    return _check_response(await client.get(url))
        except TimeoutError as exc:
            raise AuphonicApiError("Auphonic production polling timed out.") from exc
        except httpx.RequestError as exc:
            raise AuphonicApiError(f"Auphonic API request failed: {exc}") from exc

    async def _authorize_async(self, request: httpx.Request) -> None:
        _ApiOriginCredentials(self._credentials)(request)

    def delete_production(self, uuid: str) -> None:
        """Delete a production from the Auphonic account (irreversible).

        Raises :class:`AuphonicNotFoundError` when the production does not exist.
        """
        if not _PRODUCTION_UUID_RE.fullmatch(uuid):
            raise AuphonicApiError(f"Refusing to delete Auphonic production with invalid UUID {uuid!r}.")
        url = f"{self._base_url}/production/{uuid}.json"
        try:
            response = self._client.request("DELETE", url)
        except httpx.RequestError as exc:
            raise AuphonicApiError(f"Auphonic API request failed: {exc}") from exc
        if response.status_code < 300 and not response.content.strip():
            return
        _check_response(response)

    def wait_for_production(
        self,
        uuid: str,
        *,
        poll_interval: float,
        timeout_seconds: float,
        not_started_grace_seconds: float = 0.0,
        rerun_grace_seconds: float = 0.0,
    ) -> AuphonicProduction:
        """Poll until the production is done; raise on a failure status or timeout.

        ``not_started_grace_seconds`` keeps polling while a production that was
        just started still reports ``Not Started Yet``. ``rerun_grace_seconds``
        is for a production that was started again: until it reports a running
        status, its previous ``Done``, ``Production Changed`` or ``Not Started
        Yet`` status is stale, so polling continues instead of downloading the
        previous run's outputs.
        """
        _positive_finite("poll_interval", poll_interval)
        _positive_finite("timeout_seconds", timeout_seconds)
        start = time.monotonic()
        deadline = start + timeout_seconds

        seen_running = False
        while True:
            production = self.fetch_production(uuid, timeout_seconds=_poll_remaining(uuid, deadline, timeout_seconds))
            _poll_remaining(uuid, deadline, timeout_seconds)
            status = _classify_status(production.status, production.status_string)
            if status == "running":
                seen_running = True
            elif rerun_grace_seconds > 0 and not seen_running and status in {"done", "changed", "not_started"}:
                if time.monotonic() - start <= rerun_grace_seconds:
                    status = "running"
                elif status == "done":
                    raise AuphonicApiError(
                        f"Auphonic production {uuid} still reports its previous run as done "
                        f"{rerun_grace_seconds:.0f} seconds after it was started again. Check it in Auphonic; "
                        "a plain `podcast produce` follows it and downloads its outputs."
                    )
            if status == "done":
                return production
            if status == "not_started" and time.monotonic() - start <= not_started_grace_seconds:
                status = "running"
            if status != "running":
                raise AuphonicProductionFailedError(_terminal_message(uuid, status, production))
            time.sleep(min(poll_interval, _poll_remaining(uuid, deadline, timeout_seconds)))

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
        staging: Path | None = None
        keep_staging = False
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(dir=output_dir, prefix=".outputs-"))
            incoming = staging / "incoming"
            incoming.mkdir()
            filenames = asyncio.run(self._stage_outputs(outputs, incoming))
            _publish_outputs(incoming, output_dir, filenames)
            return tuple(output_dir / filename for filename in filenames)
        except _OutputRollbackError:
            keep_staging = True
            raise
        except OSError as exc:
            raise AuphonicApiError(
                f"Failed to prepare download directory or publish outputs in {output_dir}: {exc}"
            ) from exc
        finally:
            if staging is not None and not keep_staging:
                try:
                    shutil.rmtree(staging)
                except OSError as exc:
                    logging.getLogger(__name__).warning(
                        "Could not remove download staging directory %s: %s", staging, exc
                    )

    async def _stage_outputs(self, outputs: Sequence[AuphonicOutputFile], incoming: Path) -> list[str]:
        assert self._transport is None or isinstance(self._transport, httpx.AsyncBaseTransport)
        used: set[str] = set()
        filenames: list[str] = []
        async with httpx.AsyncClient(
            timeout=min(self._timeout_seconds, self._download_timeout_seconds),
            transport=self._transport,
            event_hooks={"request": [self._authorize_async]},
            headers={"Accept-Encoding": "identity"},
        ) as client:
            for idx, output in enumerate(outputs, start=1):
                filename = _unique_filename(output.filename, used, idx)
                await _download_file(
                    client,
                    output.url,
                    incoming / filename,
                    max_bytes=self._download_max_bytes,
                    timeout_seconds=self._download_timeout_seconds,
                )
                filenames.append(filename)
        return filenames


def _positive_finite(name: str, value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise AuphonicApiError(f"{name} must be finite and positive")


def _poll_remaining(uuid: str, deadline: float, timeout_seconds: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise AuphonicApiError(f"Auphonic production {uuid} timed out after {timeout_seconds} seconds.")
    return seconds


def _request_json(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = client.request(method, url, **kwargs)
    except httpx.RequestError as exc:
        raise AuphonicApiError(f"Auphonic API request failed: {exc}") from exc
    return _check_response(response)


def _check_response(response: httpx.Response) -> dict[str, Any]:
    error_class = AuphonicNotFoundError if response.status_code == 404 else AuphonicApiError
    try:
        payload = response.json()
    except ValueError as exc:
        raise error_class(f"Auphonic API returned invalid JSON (status {response.status_code}).") from exc

    if isinstance(payload, Mapping):
        status = payload.get("status")
        if isinstance(status, str) and status.lower() == "error":
            message = _extract_error_message(payload) or "Auphonic API returned an error."
            raise error_class(message)

    if response.status_code >= 400:
        message = _extract_error_message(payload) or f"HTTP {response.status_code}"
        raise error_class(f"Auphonic API error ({response.status_code}): {message}")
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
    is_multitrack = data.get("is_multitrack")
    change_allowed = data.get("change_allowed")
    return AuphonicProduction(
        uuid=uuid,
        status=status,
        status_string=status_string,
        output_files=output_files,
        is_multitrack=is_multitrack if isinstance(is_multitrack, bool) else None,
        tracks=_parse_tracks(data.get("multi_input_files")),
        input_file=_optional_str(data.get("input_file")),
        change_allowed=change_allowed if isinstance(change_allowed, bool) else None,
        chapter_titles=tuple(str(chapter.get("title") or "") for chapter in _mappings(data.get("chapters"))),
        output_formats=tuple(str(output.get("format") or "") for output in _mappings(data.get("output_files"))),
    )


def _mappings(raw: object) -> list[Mapping[str, Any]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _parse_tracks(raw: object) -> tuple[AuphonicTrack, ...]:
    tracks: list[AuphonicTrack] = []
    for item in _mappings(raw):
        track_id = _optional_str(item.get("id"))
        if track_id is None:
            continue
        tracks.append(
            AuphonicTrack(
                id=track_id,
                type=_optional_str(item.get("type")),
                input_file=_optional_str(item.get("input_file")),
            )
        )
    return tuple(tracks)


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


def _prepare_single_track(payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[tuple[str, Path]]]:
    input_files = _extract_input_files(payload)
    if len(input_files) > 1:
        raise AuphonicApiError(
            f"Auphonic single-track productions take exactly one input file (got {len(input_files)}); "
            "use a multitrack production for per-speaker stems."
        )
    body = {key: value for key, value in payload.items() if key not in {"input_file", "input_files"}}
    if not input_files:
        return body, []
    if _looks_like_url(input_files[0]):
        body["input_file"] = input_files[0]
        return body, []
    return body, [("input_file", _existing_input(input_files[0]))]


def _prepare_multitrack(payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[tuple[str, Path]]]:
    if "input_file" in payload or "input_files" in payload:
        raise AuphonicApiError("Auphonic multitrack productions take their inputs from multi_input_files only.")
    raw = payload.get("multi_input_files")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)) or not raw:
        raise AuphonicApiError("Auphonic multitrack production needs a non-empty multi_input_files list.")
    entries: list[dict[str, Any]] = []
    uploads: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise AuphonicApiError("Auphonic multi_input_files entries must be mappings.")
        entry = dict(item)
        track_id = entry.get("id")
        if not isinstance(track_id, str) or not track_id.strip():
            raise AuphonicApiError("Auphonic multi_input_files entries need a non-empty id.")
        if track_id in seen:
            raise AuphonicApiError(f"Duplicate Auphonic track id {track_id!r}.")
        seen.add(track_id)
        input_file = entry.get("input_file")
        if isinstance(input_file, str) and input_file.strip() and not _looks_like_url(input_file.strip()):
            # Local stems are uploaded after the create request, as a form field named after the track.
            uploads.append((track_id, _existing_input(input_file.strip())))
            entry.pop("input_file")
        entries.append(entry)
    body = {key: value for key, value in payload.items() if key != "multi_input_files"}
    body["multi_input_files"] = entries
    return body, uploads


def update_requests(payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split ``payload`` into a production update body and per-track updates.

    The production update carries everything except the inputs. For a
    multitrack payload each track yields ``{type, id, offset, algorithms?}``
    for ``production/{uuid}/multi_input_files/{id}.json``; a missing offset is
    sent as 0, as a new production would get it. Auphonic merges per-track
    algorithms, so a key removed from episode.yaml keeps its previous value.
    """
    body = {key: value for key, value in payload.items() if key not in _INPUT_KEYS}
    if payload.get("is_multitrack") is not True:
        return body, []
    raw = payload.get("multi_input_files")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)) or not raw:
        raise AuphonicApiError("Auphonic multitrack production needs a non-empty multi_input_files list.")
    track_updates: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise AuphonicApiError("Auphonic multi_input_files entries must be mappings.")
        track_id = item.get("id")
        if not isinstance(track_id, str) or not track_id.strip():
            raise AuphonicApiError("Auphonic multi_input_files entries need a non-empty id.")
        track: dict[str, Any] = {
            "type": item.get("type") or "multitrack",
            "id": track_id,
            "offset": item.get("offset", 0),
        }
        if "algorithms" in item:
            track["algorithms"] = item["algorithms"]
        track_updates.append(track)
    return body, track_updates


def production_state(production: AuphonicProduction) -> str:
    """``done``, ``running``, ``not_started``, ``changed`` or ``error``."""
    return _classify_status(production.status, production.status_string)


def _check_rerunnable(stored: AuphonicProduction, payload: Mapping[str, Any], *, multitrack: bool) -> None:
    """Refuse an update whose inputs differ from the stored production's uploaded ones."""
    hint = "Rerun with `podcast produce --restart` to start a new production that uploads them."
    if stored.is_multitrack is not None and stored.is_multitrack != multitrack:
        stored_kind = "multitrack" if stored.is_multitrack else "single-track"
        kind = "multitrack" if multitrack else "single-track"
        raise AuphonicApiError(
            f"Stored Auphonic production {stored.uuid} is a {stored_kind} production, but episode.yaml "
            f"configures a {kind} production. {hint}"
        )
    if production_state(stored) == "running":
        raise AuphonicApiError(
            f"Auphonic production {stored.uuid} is still running ({stored.status_string or stored.status}). "
            "Wait for it to finish; a plain `podcast produce` follows it."
        )
    if stored.change_allowed is False:
        raise AuphonicApiError(f"Auphonic does not allow changes to production {stored.uuid} right now.")
    if not multitrack:
        stored_name = _input_name(stored.input_file)
        wanted = _extract_input_files(payload)
        if wanted and stored_name and _input_name(wanted[0]) != stored_name:
            raise AuphonicApiError(
                f"Auphonic production {stored.uuid} was produced from {stored_name}, but episode.yaml "
                f"now names {_input_name(wanted[0])}. {hint}"
            )
        return
    stored_tracks = {track.id: track for track in stored.tracks if (track.type or "multitrack") == "multitrack"}
    wanted_tracks = {
        str(entry.get("id")): entry for entry in _mappings(payload.get("multi_input_files")) if entry.get("id")
    }
    if set(stored_tracks) != set(wanted_tracks):
        raise AuphonicApiError(
            f"Auphonic production {stored.uuid} has the tracks {', '.join(sorted(stored_tracks)) or 'none'}, "
            f"but episode.yaml configures {', '.join(sorted(wanted_tracks))}. {hint}"
        )
    for track_id, entry in wanted_tracks.items():
        stored_name = _input_name(stored_tracks[track_id].input_file)
        if stored_name is None:
            raise AuphonicApiError(f"Track {track_id} of Auphonic production {stored.uuid} has no input file. {hint}")
        wanted_input = entry.get("input_file")
        if isinstance(wanted_input, str) and _input_name(wanted_input) != stored_name:
            raise AuphonicApiError(
                f"Track {track_id} of Auphonic production {stored.uuid} was produced from {stored_name}, "
                f"but episode.yaml now names {_input_name(wanted_input)}. {hint}"
            )


def _check_updated(updated: AuphonicProduction, payload: Mapping[str, Any]) -> None:
    """Fail before the paid start when the update did not land as intended."""
    wanted_chapters = tuple(str(chapter.get("title") or "") for chapter in _mappings(payload.get("chapters")))
    if updated.chapter_titles != wanted_chapters:
        raise AuphonicApiError(
            f"Updating Auphonic production {updated.uuid} left {len(updated.chapter_titles)} chapters instead "
            f"of {len(wanted_chapters)}; it was not started. Check it in Auphonic."
        )
    if not updated.output_formats:
        raise AuphonicApiError(
            f"Auphonic production {updated.uuid} has no output files after the update; it was not started. "
            "Add output files to the Auphonic preset."
        )


def _input_name(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return Path(urlparse(value.strip()).path).name or None


def _track_ids(body: Mapping[str, Any]) -> list[str]:
    entries = body.get("multi_input_files")
    if not isinstance(entries, list):
        return []
    return [str(entry["id"]) for entry in entries if isinstance(entry, Mapping) and "id" in entry]


def _preset_speaker_track_ids(raw: object) -> list[str]:
    """Ids of a preset's speaker tracks; intro and outro entries are allowed in a preset."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    ids: list[str] = []
    for idx, entry in enumerate(raw, start=1):
        if not isinstance(entry, Mapping):
            continue
        entry_type = entry.get("type")
        if isinstance(entry_type, str) and entry_type.strip().lower() in {"intro", "outro"}:
            continue
        track_id = entry.get("id")
        ids.append(track_id.strip() if isinstance(track_id, str) and track_id.strip() else f"#{idx}")
    return ids


def _existing_input(raw: str) -> Path:
    path = Path(raw)
    if not path.is_file():
        raise AuphonicApiError(f"Auphonic input file not found: {path}")
    return path


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
    error = "Auphonic API returned invalid production status."
    if isinstance(status, (bool, float)):
        raise AuphonicApiError(error)
    if isinstance(status, int):
        if 0 <= status <= 999:
            return status
        raise AuphonicApiError(error)
    if isinstance(status, str) and any(char.isnumeric() for char in status):
        # Any numeric-looking text ("-1", "3.0", "1e3", "²") must be a plain code.
        value = status.strip()
        if not (value.isascii() and value.isdigit() and len(value) <= 3):
            raise AuphonicApiError(error)
        return int(value)
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
    hint = (
        "Fix the cause, then rerun with `podcast produce --update` to run it again without uploading "
        "the inputs, or `podcast produce --restart` to start a new production."
    )
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
    if name in {"", ".", ".."}:
        name = f"output_{idx}"
    if name.casefold() not in used:
        used.add(name.casefold())
        return name
    stem = Path(name).stem
    suffix = Path(name).suffix
    counter = 2
    while True:
        candidate = f"{stem}_{counter}{suffix}"
        if candidate.casefold() not in used:
            used.add(candidate.casefold())
            return candidate
        counter += 1


async def _download_file(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    max_bytes: int,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    try:
        async with asyncio.timeout(timeout_seconds):
            request = client.build_request("GET", url)
            # Follow redirects without buffering their bodies. The origin hook still
            # authorizes each hop independently and the total deadline covers the chain.
            for _ in range(client.max_redirects + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                request.extensions["timeout"] = httpx.Timeout(
                    min(client.timeout.read or remaining, remaining)
                ).as_dict()
                response = await client.send(request, stream=True, follow_redirects=False)
                try:
                    if response.is_redirect and response.next_request is not None:
                        request = response.next_request
                        continue
                    _check_download_response(response, max_bytes, dest.name)
                    await _atomic_write_stream(dest, response.aiter_bytes(), max_bytes=max_bytes, deadline=deadline)
                    return
                finally:
                    await response.aclose()
            raise AuphonicApiError("Failed to download output: too many redirects")
    except TimeoutError as exc:
        raise AuphonicApiError(f"Failed to download {dest.name}: download deadline exceeded") from exc
    except OSError as exc:
        raise AuphonicApiError(f"Failed to download {dest.name}: {exc}") from exc
    except httpx.HTTPError as exc:
        # Signed URLs and transport exception messages never enter diagnostics.
        raise AuphonicApiError(f"Failed to download {dest.name}: {type(exc).__name__}") from exc


def _check_download_response(response: httpx.Response, max_bytes: int, filename: str) -> None:
    if response.status_code >= 400:
        raise AuphonicApiError(f"Failed to download {filename} (status {response.status_code})")
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise AuphonicApiError(f"Failed to download {filename}: unsupported Content-Encoding")
    length = response.headers.get("Content-Length", "")
    if length.isdecimal():
        try:
            too_large = int(length) > max_bytes
        except ValueError as exc:
            raise AuphonicApiError(f"Failed to download {filename}: invalid Content-Length") from exc
        if too_large:
            raise AuphonicApiError(f"Failed to download {filename}: byte limit exceeded")


async def _atomic_write_stream(path: Path, chunks: AsyncIterable[bytes], *, max_bytes: int, deadline: float) -> None:
    tmp_path: Path | None = None
    total = 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as tmp:
            tmp_path = Path(tmp.name)
            async for chunk in chunks:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                total += len(chunk)
                if total > max_bytes:
                    raise AuphonicApiError(f"Failed to download {path.name}: byte limit exceeded")
                tmp.write(chunk)
            tmp.flush()
            os.fsync(tmp.fileno())
        if time.monotonic() >= deadline:
            raise TimeoutError
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError as exc:
                logging.getLogger(__name__).warning("Could not remove download temporary file %s: %s", tmp_path, exc)


def _publish_outputs(incoming: Path, output_dir: Path, filenames: Sequence[str]) -> None:
    backup = incoming.parent / "previous"
    backup.mkdir()
    published: list[str] = []
    saved: list[str] = []
    try:
        for name in filenames:
            dest = output_dir / name
            if dest.is_dir() and not dest.is_symlink():
                raise OSError(f"Refusing to replace directory {dest}")
            if dest.exists() or dest.is_symlink():
                os.replace(dest, backup / name)
                saved.append(name)
            os.replace(incoming / name, dest)
            published.append(name)
    except BaseException:
        try:
            _restore_outputs(output_dir, backup, published, saved)
        except OSError as exc:
            raise _OutputRollbackError(
                f"Output publication rollback failed; recovery files retained in {incoming.parent}"
            ) from exc
        raise


def _restore_outputs(output_dir: Path, backup: Path, published: Sequence[str], saved: Sequence[str]) -> None:
    errors: list[OSError] = []
    for name in reversed(published):
        try:
            (output_dir / name).unlink()
        except OSError as exc:
            errors.append(exc)
    for name in reversed(saved):
        try:
            os.replace(backup / name, output_dir / name)
        except OSError as exc:
            errors.append(exc)
    if errors:
        raise errors[0]
