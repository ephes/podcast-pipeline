"""Client for django-cast's editor API (``/api/editor/``), token-authenticated.

The token is only ever sent to the configured https base URL: every request URL
is built from it, and redirects are never followed (a redirect is an error), so
the ``Authorization`` header cannot leak to another host.

Two operations depend on editor endpoints django-cast does not offer yet. They
are feature-detected with an ``OPTIONS`` request, so a django-cast release that
adds them is used without a change here:

- :data:`AUDIO_DETAIL_PATH` with ``PATCH``: add a further file format (one
  multipart file per request, field ``m4a``/``oga``/``opus``) to an existing
  Audio. Until then those formats are added by hand in the Wagtail admin.
  The same ``PATCH`` (form field ``transcript_diarization_mode``) sets the
  diarization mode of an Audio that was uploaded without it.
- :data:`EPISODE_TRANSCRIPT_PATH` with ``POST``: enqueue Voxhelm transcript
  generation for an episode's podcast audio. Until then "Generate transcript"
  in the Wagtail episode editor starts it.

Two more are detected from the API's answers instead:

- ``transcript_diarization_mode=enabled`` on the audio upload, which django-cast
  rejects today: probed with a file-less upload request that creates nothing.
- An episode ``contributors`` field (``[{"slug": ..., "role": "host"|"guest"}]``):
  sent in a follow-up ``PATCH`` once episode responses contain ``contributors``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx

from podcast_pipeline.cms_draft import AudioFile, AudioUpload, validate_base_url

PARENTS_PATH = "api/editor/parents/"
POST_LOOKUP_PATH = "api/editor/posts/"
EPISODES_PATH = "api/editor/episodes/"
EPISODE_DETAIL_PATH = "api/editor/episodes/{id}/"
EPISODE_PUBLISH_PATH = "api/editor/episodes/{id}/publish/"
AUDIOS_PATH = "api/editor/media/audios/"
AUDIO_DETAIL_PATH = "api/editor/media/audios/{id}/"
EPISODE_TRANSCRIPT_PATH = "api/editor/episodes/{id}/transcript/"

_AUDIO_CONTENT_TYPES = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "oga": "audio/ogg", "opus": "audio/ogg"}
_AUDIO_PAGE_SIZE = 200


class CmsApiError(RuntimeError):
    pass


class CmsRevisionConflictError(CmsApiError):
    """The draft changed (for example in Wagtail) since its revision was read."""


def load_cms_token(env_name: str) -> str:
    token = (os.environ.get(env_name) or "").strip()
    if not token:
        raise CmsApiError(
            f"Missing django-cast API token: set {env_name} to a token of a user with Wagtail admin access "
            "(cms.token_env names the variable)."
        )
    return token


class DjangoCastClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 60.0,
        upload_timeout_seconds: float = 1800.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        validate_base_url(base_url)
        self.base_url = base_url.rstrip("/") + "/"
        self._upload_timeout = upload_timeout_seconds
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
            headers={"Authorization": f"Token {token}", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DjangoCastClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def absolute_url(self, path: str | None) -> str | None:
        """Turn a site-relative URL from a response (``/cms/pages/1/edit/``) into one on the base URL."""
        if not path:
            return None
        if path.startswith(("http://", "https://")):
            return path
        return self.base_url + path.lstrip("/")

    def list_parents(self) -> list[dict[str, Any]]:
        data = self._json("GET", PARENTS_PATH)
        if not isinstance(data, list):
            raise CmsApiError(f"GET {PARENTS_PATH} returned {type(data).__name__}, expected a list")
        return [item for item in data if isinstance(item, dict)]

    def find_post(self, *, parent_id: int, slug: str) -> dict[str, Any] | None:
        """The page under ``parent_id`` whose latest draft has ``slug``, or ``None``."""
        return self._json_or_none("GET", POST_LOOKUP_PATH, params={"parent": str(parent_id), "slug": slug})

    def get_episode(self, episode_id: int) -> dict[str, Any] | None:
        return self._json_or_none("GET", EPISODE_DETAIL_PATH.format(id=episode_id))

    def upload_audio(self, upload: AudioUpload) -> dict[str, Any]:
        """Create an Audio from one file (django-cast accepts exactly one file per upload)."""
        return self._upload("POST", AUDIOS_PATH, upload.file, data=upload.form_fields())

    def find_audio(self, audio_id: int, *, max_pages: int = 50) -> dict[str, Any] | None:
        """Audio ``audio_id`` from the editor audio list, newest first (there is no audio detail endpoint).

        Pages are requested by number on the configured base URL; the ``next`` links in responses are never
        followed, so the token stays on the configured site.
        """
        for page in range(1, max_pages + 1):
            data = self._json("GET", AUDIOS_PATH, params={"page": str(page), "pageSize": str(_AUDIO_PAGE_SIZE)})
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list) or not results:
                return None
            for item in results:
                if isinstance(item, dict) and item.get("id") == audio_id:
                    return item
            if not data.get("next"):
                return None
        return None

    def create_episode(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._dict("POST", EPISODES_PATH, json=dict(payload))

    def update_episode(self, episode_id: int, payload: Mapping[str, Any], *, base_revision_id: int) -> dict[str, Any]:
        """PATCH the draft; refused when it changed since ``base_revision_id`` or went live."""
        body = {**payload, "base_revision_id": base_revision_id, "require_unpublished": True}
        return self._dict("PATCH", EPISODE_DETAIL_PATH.format(id=episode_id), json=body)

    def publish_episode(self, episode_id: int, *, revision_id: int) -> dict[str, Any]:
        return self._dict(
            "POST",
            EPISODE_PUBLISH_PATH.format(id=episode_id),
            headers={"If-Match": f'"{revision_id}"'},
        )

    def supports(self, method: str, path: str) -> bool:
        """Whether the site serves ``method`` on ``path``, from an ``OPTIONS`` request's ``Allow`` header."""
        response = self._send("OPTIONS", path)
        if response.status_code in {404, 405, 501}:
            return False
        if response.status_code >= 400:
            raise _error(response, "OPTIONS", path)
        allowed = {item.strip().upper() for item in response.headers.get("Allow", "").split(",")}
        return method.upper() in allowed

    def supports_adding_audio_files(self, audio_id: int) -> bool:
        return self.supports("PATCH", AUDIO_DETAIL_PATH.format(id=audio_id))

    def add_audio_file(self, audio_id: int, audio_file: AudioFile) -> dict[str, Any]:
        return self._upload("PATCH", AUDIO_DETAIL_PATH.format(id=audio_id), audio_file, data={})

    def supports_upload_diarization_mode(self, mode: str, *, collection: int | None) -> bool:
        """Whether an audio upload accepts ``transcript_diarization_mode=mode``, probed without a file.

        django-cast checks the diarization mode before it requires a file, so the file-less request is
        rejected either for the mode (not supported) or for the missing file (supported); nothing is created.
        """
        data = {"transcript_diarization_mode": mode}
        if collection is not None:
            data["collection"] = str(collection)
        response = self._send("POST", AUDIOS_PATH, data=data)
        if response.status_code < 400:
            raise CmsApiError(
                f"POST {AUDIOS_PATH} without a file was accepted (HTTP {response.status_code}); "
                "check the site's audio list for an empty Audio."
            )
        if response.status_code != 400:
            raise _error(response, "POST", AUDIOS_PATH)
        try:
            body = response.json()
        except ValueError:
            return False
        errors = body.get("errors") if isinstance(body, dict) else None
        return isinstance(errors, Mapping) and "transcript_diarization_mode" not in errors

    def set_audio_diarization_mode(self, audio_id: int, mode: str) -> dict[str, Any]:
        path = AUDIO_DETAIL_PATH.format(id=audio_id)
        return self._dict("PATCH", path, data={"transcript_diarization_mode": mode})

    def supports_transcript_generation(self, episode_id: int) -> bool:
        return self.supports("POST", EPISODE_TRANSCRIPT_PATH.format(id=episode_id))

    def start_transcript_generation(self, episode_id: int) -> dict[str, Any] | None:
        data = self._json("POST", EPISODE_TRANSCRIPT_PATH.format(id=episode_id))
        return data if isinstance(data, dict) else None

    def _upload(self, method: str, path: str, audio_file: AudioFile, *, data: Mapping[str, str]) -> dict[str, Any]:
        content_type = _AUDIO_CONTENT_TYPES.get(audio_file.field, "application/octet-stream")
        try:
            with audio_file.path.open("rb") as handle:
                files = {audio_file.field: (audio_file.path.name, handle, content_type)}
                response = self._send(method, path, data=dict(data), files=files, timeout=self._upload_timeout)
        except OSError as exc:
            raise CmsApiError(f"Could not read {audio_file.path}: {exc}") from exc
        return _expect_dict(_checked(response, method, path), method, path)

    def _dict(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return _expect_dict(self._json(method, path, **kwargs), method, path)

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return _checked(self._send(method, path, **kwargs), method, path)

    def _json_or_none(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | None:
        response = self._send(method, path, **kwargs)
        if response.status_code == 404:
            return None
        return _expect_dict(_checked(response, method, path), method, path)

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = self.base_url + path
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise CmsApiError(f"{method} {url} failed: {exc}") from exc
        if response.is_redirect:
            location = response.headers.get("Location", "?")
            raise CmsApiError(
                f"{method} {url} was redirected to {location}; redirects are not followed so the API token stays "
                "on the configured site. Check cms.base_url (scheme, host, trailing path)."
            )
        return response


def _checked(response: httpx.Response, method: str, path: str) -> Any:
    if response.status_code >= 400:
        raise _error(response, method, path)
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise CmsApiError(f"{method} {path} returned non-JSON content (HTTP {response.status_code})") from exc


def _expect_dict(data: Any, method: str, path: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise CmsApiError(f"{method} {path} returned {type(data).__name__}, expected an object")
    return data


def _error(response: httpx.Response, method: str, path: str) -> CmsApiError:
    try:
        body = response.json()
    except ValueError:
        body = None
    prefix = f"{method} {path} failed with HTTP {response.status_code}"
    if not isinstance(body, dict):
        text = response.text.strip()[:300]
        return CmsApiError(f"{prefix}: {text}" if text else prefix)
    code = body.get("code")
    if code == "revision_conflict":
        return CmsRevisionConflictError(
            f"{prefix}: the draft changed since it was read (current revision "
            f"{body.get('current_revision_id')}). Check the edit in Wagtail; a re-run "
            "replaces it with the workspace copy."
        )
    message = f"{prefix}: {code}" if code else prefix
    detail = body.get("detail")
    if detail:
        message += f": {detail}"
    errors = body.get("errors")
    if isinstance(errors, Mapping):
        for field, items in errors.items():
            for item in items if isinstance(items, list) else [items]:
                item_message = item.get("message") if isinstance(item, Mapping) else item
                message += f"\n  {field}: {item_message}"
    return CmsApiError(message)


def audio_file_label(audio_file: AudioFile) -> str:
    return f"{audio_file.field} ({audio_file.path.name})"
