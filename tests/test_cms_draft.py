"""`podcast cms-draft` / `cms-publish` against a mocked django-cast editor API.

No real site is ever called: every request goes through ``httpx.MockTransport``
backed by a small in-memory fake of the editor endpoints.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import typer
import yaml
from typer.testing import CliRunner

from podcast_pipeline.cms_api import CmsApiError, DjangoCastClient
from podcast_pipeline.cms_draft import (
    CmsConfig,
    CmsConfigError,
    ContributorRef,
    build_cms_draft_plan,
    link_hosts,
    load_cms_config,
)
from podcast_pipeline.entrypoints import cms_draft as cms_entry
from podcast_pipeline.entrypoints.cli import app

BASE = "https://cms.test/"
TOKEN = "secret-token"
PODCAST_ID = 120
AUDIO_FIELDS = ("mp3", "m4a", "oga", "opus")
HOST_LINKS = {
    "Jochen": "https://fedi.example/@jochen",
    "Dominik": "https://mastodon.example/@dominik",
    "Johannes": "https://mastodon.example/@johannes",
}
CONTRIBUTORS = {"Jochen": "jochen", "Dominik": {"slug": "dominik", "role": "host"}}
INTRO = '<h2>Shownotes</h2><p>Mail: <a href="mailto:hallo@example.org">hallo@example.org</a></p>'


class FakeCast:
    """In-memory django-cast editor API: parents, post lookup, episodes, audio uploads."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.episodes: dict[int, dict[str, Any]] = {}
        self.audios: dict[int, dict[str, Any]] = {}
        self.other_posts: dict[str, dict[str, Any]] = {}
        self.next_id = 141
        self.next_audio_id = 83
        self.revision = 760
        self.options_allow: dict[str, str] = {}
        self.fail_episode_create = False
        self.redirect_all = False
        self.accept_enabled_diarization = False
        self.supports_contributors = False
        self.contributors_read_only = False
        self.list_page_size_cap = 200
        self.hidden_audios: set[int] = set()
        self.fail_episode_patch = False
        self.diarization_read_only = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.redirect_all:
            return httpx.Response(301, headers={"Location": "https://elsewhere.test/api/"})
        assert request.headers["Authorization"] == f"Token {TOKEN}"
        path = request.url.path
        if request.method == "OPTIONS":
            allow = self.options_allow.get(path)
            if allow is None:
                return httpx.Response(404, json={"code": "not_found", "detail": "Not found."})
            return httpx.Response(200, headers={"Allow": allow}, json={})
        if path == "/api/editor/parents/":
            return httpx.Response(
                200,
                json=[
                    {"id": 2, "title": "Blog", "type": "cast.Blog", "api_url": "/api/editor/posts/"},
                    {"id": PODCAST_ID, "title": "Podcast", "type": "cast.Podcast", "api_url": "/api/editor/episodes/"},
                ],
            )
        if path == "/api/editor/posts/" and request.method == "GET":
            return self._lookup(request)
        if path.startswith("/api/editor/media/audios/"):
            return self._audio_route(request, path)
        if path == "/api/editor/episodes/" and request.method == "POST":
            return self._create(request)
        match = re.fullmatch(r"/api/editor/episodes/(\d+)/(publish/|transcript/)?", path)
        if match:
            return self._episode_route(request, int(match.group(1)), match.group(2))
        return httpx.Response(404, json={"code": "not_found", "detail": "Not found."})

    def _audio_route(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/api/editor/media/audios/" and request.method == "POST":
            return self._upload(request)
        if path == "/api/editor/media/audios/" and request.method == "GET":
            return self._list_audios(request)
        if re.fullmatch(r"/api/editor/media/audios/\d+/", path) and request.method == "PATCH":
            return self._patch_audio(int(path.split("/")[-2]), request)
        return httpx.Response(404, json={"code": "not_found", "detail": "Not found."})

    def _episode_route(self, request: httpx.Request, episode_id: int, action: str | None) -> httpx.Response:
        episode = self.episodes.get(episode_id)
        if episode is None:
            return httpx.Response(404, json={"code": "not_found", "detail": "Episode not found."})
        if action == "publish/":
            assert request.headers["If-Match"] == f'"{episode["latest_revision_id"]}"'
            episode.update(live=True, status="live")
            return httpx.Response(200, json={**episode, "public_url": "/podcast/" + episode["slug"] + "/"})
        if action == "transcript/":
            return httpx.Response(202, json={"status": "queued"})
        if request.method == "PATCH":
            return self._patch(episode, request)
        return httpx.Response(200, json=self._episode_json(episode))

    def _episode_json(self, episode: dict[str, Any]) -> dict[str, Any]:
        data = {key: value for key, value in episode.items() if key != "contributors"}
        if self.supports_contributors:
            data["contributors"] = [
                {"contributor": {"slug": item["slug"]}, "role": item["role"]}
                for item in episode.get("contributors", [])
            ]
        return data

    def _accepted(self, body: dict[str, Any]) -> dict[str, Any]:
        """Like DRF, ignore fields the (simulated) serializer does not know or exposes read-only."""
        if self.supports_contributors and not self.contributors_read_only:
            return body
        return {key: value for key, value in body.items() if key != "contributors"}

    def _audio_json(self, audio_id: int) -> dict[str, Any]:
        audio = self.audios[audio_id]
        files = {field.decode(): name.decode() for field, name in [audio["file"], *audio.get("added", [])]}
        return {
            "id": audio_id,
            "transcript_diarization_mode": audio["mode"],
            "edit_url": f"/cms/audio/audio/edit/{audio_id}/",
            **{field: (f"/cast_audio/{files[field]}" if field in files else None) for field in AUDIO_FIELDS},
        }

    def _list_audios(self, request: httpx.Request) -> httpx.Response:
        assert set(request.url.params) <= {"page", "pageSize", "q", "tag"}
        size = min(int(request.url.params.get("pageSize", "40")), self.list_page_size_cap)
        page = int(request.url.params.get("page", "1"))
        ids = [audio_id for audio_id in sorted(self.audios, reverse=True) if audio_id not in self.hidden_audios]
        chunk = ids[(page - 1) * size : page * size]
        more = page * size < len(ids)
        return httpx.Response(
            200,
            json={
                "count": len(ids),
                "next": f"https://elsewhere.test/api/editor/media/audios/?page={page + 1}" if more else None,
                "previous": None,
                "results": [self._audio_json(audio_id) for audio_id in chunk],
            },
        )

    def _patch_audio(self, audio_id: int, request: httpx.Request) -> httpx.Response:
        body = request.read()
        if request.headers["Content-Type"].startswith("application/x-www-form-urlencoded"):
            if not self.diarization_read_only:
                self.audios[audio_id]["mode"] = parse_qs(body.decode())["transcript_diarization_mode"][0]
        else:
            self.audios[audio_id].setdefault("added", []).extend(
                re.findall(rb'name="(\w+)"; filename="([^"]+)"', body)
            )
        return httpx.Response(200, json=self._audio_json(audio_id))

    def _lookup(self, request: httpx.Request) -> httpx.Response:
        assert set(request.url.params) == {"parent", "slug"}
        slug = request.url.params["slug"]
        if slug in self.other_posts:
            return httpx.Response(200, json=self.other_posts[slug])
        for episode in self.episodes.values():
            if episode["slug"] == slug and episode["parent"]["id"] == int(request.url.params["parent"]):
                return httpx.Response(200, json={key: episode[key] for key in ("id", "type", "slug", "edit_url")})
        return httpx.Response(404, json={"code": "not_found", "detail": "Post not found."})

    def _upload(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        if request.headers["Content-Type"].startswith("application/x-www-form-urlencoded"):
            fields = {key: values[0] for key, values in parse_qs(body.decode()).items()}
            files = []
        else:
            pattern = (
                rb'name="(title|subtitle|chaptermarks|collection|transcript_diarization_mode)"\r\n\r\n(.*?)\r\n--'
            )
            fields = {k.decode(): v.decode() for k, v in re.findall(pattern, body, re.S)}
            files = re.findall(rb'name="(mp3|m4a|oga|opus)"; filename="([^"]+)"', body)
        # django-cast checks the diarization mode before it requires a file.
        if fields.get("transcript_diarization_mode") == "enabled" and not self.accept_enabled_diarization:
            return _validation_error(
                "transcript_diarization_mode", "unsupported", "Enabled diarization is not supported."
            )
        if len(files) != 1:
            return _validation_error("non_field_errors", "required", "Submit one audio file.")
        audio_id = self.next_audio_id
        self.next_audio_id += 1
        mode = fields.pop("transcript_diarization_mode", "inherit")
        self.audios[audio_id] = {"file": files[0], "fields": fields, "mode": mode}
        return httpx.Response(201, json=self._audio_json(audio_id))

    def _create(self, request: httpx.Request) -> httpx.Response:
        if self.fail_episode_create:
            return httpx.Response(
                400,
                json={
                    "code": "validation_error",
                    "errors": {"slug": [{"code": "duplicate", "message": "Slug 'python-315' is already used here."}]},
                },
            )
        body = self._accepted(json.loads(request.content))
        episode_id = self.next_id
        self.next_id += 1
        self.revision += 1
        self.episodes[episode_id] = {
            **{key: value for key, value in body.items() if key != "parent"},
            "id": episode_id,
            "type": "cast.Episode",
            "parent": body["parent"],
            "latest_revision_id": self.revision,
            "live": False,
            "status": "draft",
            "preview_url": f"/cms/pages/{episode_id}/view_draft/",
            "edit_url": f"/cms/pages/{episode_id}/edit/",
        }
        return httpx.Response(201, json=self._episode_json(self.episodes[episode_id]))

    def _patch(self, episode: dict[str, Any], request: httpx.Request) -> httpx.Response:
        if self.fail_episode_patch:
            return httpx.Response(409, json={"code": "page_locked", "detail": "This page is locked for editing."})
        body = self._accepted(json.loads(request.content))
        if body["base_revision_id"] != episode["latest_revision_id"]:
            return httpx.Response(
                409, json={"code": "revision_conflict", "current_revision_id": episode["latest_revision_id"]}
            )
        assert body["require_unpublished"] is True
        if episode["live"]:
            return httpx.Response(409, json={"code": "published_post", "detail": "This episode is already live."})
        self.revision += 1
        episode.update({key: value for key, value in body.items() if key not in {"base_revision_id"}})
        episode["latest_revision_id"] = self.revision
        return httpx.Response(200, json=self._episode_json(episode))

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == method and request.url.path == path]


def _validation_error(field: str, code: str, message: str) -> httpx.Response:
    return httpx.Response(
        400, json={"code": "validation_error", "errors": {field: [{"code": code, "message": message}]}}
    )


def _write_selected(workspace: Path, name: str, text: str) -> None:
    path = workspace / "copy" / "selected" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _workspace(tmp_path: Path, *, extra_formats: bool = True) -> Path:
    workspace = tmp_path / "pp_73"
    workspace.mkdir()
    episode_yaml = {
        "schema_version": 1,
        "episode_id": "pp_73",
        "hosts": ["Jochen", "Dominik"],
        "auphonic": {
            "chapters": [
                {"start": 0, "title": "Begrüßung"},
                {"start": 43.6, "title": "Python   3.15"},
                {"start": "01:03:24", "title": "Picks"},
            ]
        },
    }
    (workspace / "episode.yaml").write_text(yaml.safe_dump(episode_yaml, allow_unicode=True), encoding="utf-8")
    _write_selected(workspace, "title_detail.txt", "Was ist neu in Python 3.15?\n")
    _write_selected(workspace, "title_seo.txt", "Python 3.15: Neuerungen\n")
    _write_selected(workspace, "slug.txt", "python-315\n")
    _write_selected(workspace, "summary_short.txt", "Kurze Zusammenfassung.\n")
    _write_selected(workspace, "subtitle_auphonic.txt", "Untertitel fürs Audio\n")
    _write_selected(workspace, "cms_tags.md", "# CMS tags\n\n- Python 3.15\n- FrozenDict\n")
    _write_selected(workspace, "itunes_keywords.md", "# iTunes keywords\n\n- python\n- lazy imports\n")
    _write_selected(
        workspace,
        "description.md",
        "## Python 3.15 ist da!\n\nJochen, Dominik und Johannes sind zurück.\n\n- **FrozenDict**: endlich\n",
    )
    _write_selected(workspace, "shownotes.md", "## Links\n\n- [PEP 814](https://peps.python.org/pep-0814/)\n")
    outputs = workspace / "auphonic" / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "pp_73.mp3").write_bytes(b"ID3 fake mp3")
    if extra_formats:
        (outputs / "pp_73.m4a").write_bytes(b"m4a")
        (outputs / "pp_73.ogg").write_bytes(b"ogg")
        (outputs / "pp_73.opus").write_bytes(b"opus")
    return workspace


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Path:
    cms: dict[str, Any] = {
        "base_url": BASE,
        "token_env": "TEST_CMS_TOKEN",
        "parent_id": PODCAST_ID,
        "audio_collection": 1,
        "detail_intro_html": INTRO,
        "host_links": HOST_LINKS,
        "contributors": CONTRIBUTORS,
        **overrides,
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"cms": cms}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(path))
    monkeypatch.setenv("TEST_CMS_TOKEN", TOKEN)
    return path


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeCast:
    server = FakeCast()

    def make_client(base_url: str, token: str) -> DjangoCastClient:
        return DjangoCastClient(base_url, token, transport=server.transport())

    monkeypatch.setattr(cms_entry, "DjangoCastClient", make_client)
    return server


def _run(workspace: Path, **kwargs: Any) -> None:
    cms_entry.run_cms_draft(workspace=workspace, dry_run=False, **kwargs)


def _plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: Path) -> Any:
    _config(tmp_path, monkeypatch)
    episode_yaml = yaml.safe_load((workspace / "episode.yaml").read_text(encoding="utf-8"))
    return build_cms_draft_plan(episode_yaml=episode_yaml, workspace=workspace, config=load_cms_config(episode_yaml))


def test_plan_builds_episode_payload_with_audio_block_and_shownotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch, _workspace(tmp_path))
    payload = plan.episode_payload(83)

    assert payload["title"] == "Was ist neu in Python 3.15?"
    assert payload["slug"] == "python-315"
    assert payload["seo_title"] == "Python 3.15: Neuerungen"
    assert payload["search_description"] == "Kurze Zusammenfassung."
    assert payload["tags"] == ["Python 3.15", "FrozenDict"]
    assert payload["keywords"] == "python,lazy imports"
    assert payload["explicit"] == 2
    assert payload["podcast_audio"] == {"id": 83}
    overview_text, audio_block = payload["overview"]
    assert audio_block == {"type": "audio", "value": {"id": 83}}
    assert overview_text["type"] == "paragraph"
    assert "Python 3.15 ist da!" not in overview_text["value"]
    assert overview_text["value"].startswith(
        f'<p><a href="{HOST_LINKS["Jochen"]}">Jochen</a>, <a href="{HOST_LINKS["Dominik"]}">Dominik</a> und Johannes'
    )
    (detail,) = payload["detail"]
    assert detail["value"].startswith(INTRO + "<h2>Links</h2>")
    assert 'href="https://peps.python.org/pep-0814/"' in detail["value"]

    assert plan.audio.form_fields() == {
        "title": "Was ist neu in Python 3.15?",
        "subtitle": "Untertitel fürs Audio",
        "chaptermarks": "00:00:00 Begrüßung\n00:00:43 Python 3.15\n01:03:24 Picks",
        "collection": "1",
        "transcript_diarization_mode": "enabled",
    }
    assert plan.contributors_payload() == [{"slug": "jochen", "role": "host"}, {"slug": "dominik", "role": "host"}]
    assert plan.unmapped_hosts == ()
    assert plan.audio.file.field == "mp3"
    assert [(item.field, item.path.name) for item in plan.extra_audio_files] == [
        ("m4a", "pp_73.m4a"),
        ("oga", "pp_73.ogg"),
        ("opus", "pp_73.opus"),
    ]


def test_plan_uses_selected_html_description_and_drops_leading_heading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "copy" / "selected" / "description.md").unlink()
    _write_selected(workspace, "description.html", "<h1>Titel</h1>\n<p>Text von Jochen.</p>")
    plan = _plan(tmp_path, monkeypatch, workspace)
    assert plan.overview_html == f'<p>Text von <a href="{HOST_LINKS["Jochen"]}">Jochen</a>.</p>'


def test_plan_requires_selected_copy_and_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "auphonic" / "outputs" / "pp_73.mp3").unlink()
    with pytest.raises(CmsConfigError, match="No MP3 found"):
        _plan(tmp_path, monkeypatch, workspace)
    (workspace / "copy" / "selected" / "slug.txt").write_text("Python 3.15\n", encoding="utf-8")
    with pytest.raises(CmsConfigError, match="Selected slug"):
        _plan(tmp_path, monkeypatch, workspace)


def test_audio_file_override_from_episode_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path, extra_formats=False)
    (workspace / "master.m4a").write_bytes(b"m4a")
    data = yaml.safe_load((workspace / "episode.yaml").read_text(encoding="utf-8"))
    data["cms"] = {"audio_file": "master.m4a"}
    (workspace / "episode.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    plan = _plan(tmp_path, monkeypatch, workspace)
    assert (plan.audio.file.field, plan.audio.file.path) == ("m4a", workspace / "master.m4a")
    assert [item.field for item in plan.extra_audio_files] == []


def test_link_hosts_skips_existing_links_and_anchor_text() -> None:
    html_text = (
        f'<p><a href="https://example.org/jochen-talk">Jochen spricht</a> mit Dominik. Dominik und Jochen.</p>'
        f'<p><a href="{HOST_LINKS["Johannes"]}">Johannes</a></p>'
    )
    linked = link_hosts(html_text, hosts=None, host_links=HOST_LINKS)
    assert linked == (
        f'<p><a href="https://example.org/jochen-talk">Jochen spricht</a> mit <a href="{HOST_LINKS["Dominik"]}">'
        f'Dominik</a>. Dominik und <a href="{HOST_LINKS["Jochen"]}">Jochen</a>.</p>'
        f'<p><a href="{HOST_LINKS["Johannes"]}">Johannes</a></p>'
    )
    assert link_hosts("<p>Jochens Folge mit Dominik</p>", hosts=["Jochen"], host_links=HOST_LINKS) == (
        "<p>Jochens Folge mit Dominik</p>"
    )


@pytest.mark.parametrize("base_url", ["http://cms.test/", "https://user:pw@cms.test/", "cms.test"])
def test_config_requires_plain_https_base_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    _config(tmp_path, monkeypatch, base_url=base_url)
    with pytest.raises(CmsConfigError, match="cms.base_url"):
        load_cms_config({})


def test_config_requires_base_url_and_merges_episode_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(CmsConfigError, match="Missing cms.base_url"):
        load_cms_config({})
    _config(tmp_path, monkeypatch)
    config = load_cms_config(
        {
            "cms": {
                "explicit": 1,
                "host_links": {"Gast": "https://gast.example/"},
                "contributors": {"Gast": {"slug": "gast", "role": "guest"}},
            }
        }
    )
    assert config == CmsConfig(
        base_url=BASE,
        token_env="TEST_CMS_TOKEN",
        parent_id=PODCAST_ID,
        audio_collection=1,
        explicit=1,
        detail_intro_html=INTRO,
        host_links={**HOST_LINKS, "Gast": "https://gast.example/"},
        transcript_diarization="enabled",
        contributors={
            "Jochen": ContributorRef(slug="jochen"),
            "Dominik": ContributorRef(slug="dominik"),
            "Gast": ContributorRef(slug="gast", role="guest"),
        },
    )
    with pytest.raises(CmsConfigError, match="role in episode.yaml must be host or guest"):
        load_cms_config({"cms": {"contributors": {"Gast": {"slug": "gast", "role": "moderator"}}}})
    with pytest.raises(CmsConfigError, match="transcript_diarization"):
        load_cms_config({"cms": {"transcript_diarization": "on"}})


def test_dry_run_prints_payloads_without_token_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch, parent_id=None)
    monkeypatch.delenv("TEST_CMS_TOKEN")

    def no_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry run must not create a client")

    monkeypatch.setattr(cms_entry, "DjangoCastClient", no_client)
    cms_entry.run_cms_draft(workspace=workspace, dry_run=True)

    out = json.loads(capsys.readouterr().out)
    create = out["episode_create"]["body"]
    assert create["parent"] == {"id": "<resolved from GET api/editor/parents/>"}
    assert create["podcast_audio"] == {"id": "<audio id>"}
    assert create["overview"][1] == {"type": "audio", "value": {"id": "<audio id>"}}
    assert out["audio_upload"]["file"] == {"mp3": str(workspace / "auphonic" / "outputs" / "pp_73.mp3")}
    assert out["audio_upload"]["fields"]["chaptermarks"].startswith("00:00:00 Begrüßung")
    assert sorted(out["extra_audio_files"]) == ["m4a", "oga", "opus"]
    assert out["episode_update"]["body"]["require_unpublished"] is True
    assert out["audio_upload"]["fields"]["transcript_diarization_mode"] == "enabled"
    assert out["contributors"] == [{"slug": "jochen", "role": "host"}, {"slug": "dominik", "role": "host"}]
    assert out["hosts_without_contributor"] == []
    assert not (workspace / "cms").exists()


def test_creates_draft_uploads_one_file_and_lists_manual_steps_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _run(workspace)

    probe, upload = fake.calls("POST", "/api/editor/media/audios/")
    assert parse_qs(probe.read().decode()) == {"transcript_diarization_mode": ["enabled"], "collection": ["1"]}
    assert b'name="transcript_diarization_mode"' not in upload.read()
    assert fake.audios == {
        83: {
            "file": (b"mp3", b"pp_73.mp3"),
            "fields": {
                "title": "Was ist neu in Python 3.15?",
                "subtitle": "Untertitel fürs Audio",
                "chaptermarks": "00:00:00 Begrüßung\n00:00:43 Python 3.15\n01:03:24 Picks",
                "collection": "1",
            },
            "mode": "inherit",
        }
    }
    (create,) = fake.calls("POST", "/api/editor/episodes/")
    body = json.loads(create.content)
    assert body["parent"] == {"id": PODCAST_ID}
    assert body["podcast_audio"] == {"id": 83}
    assert body["overview"][1] == {"type": "audio", "value": {"id": 83}}
    assert "publish" not in body
    assert "contributors" not in body
    assert not [request for request in fake.requests if request.url.path.endswith("/publish/")]

    state = json.loads((workspace / "cms" / "draft.json").read_text(encoding="utf-8"))
    assert state == {
        "base_url": BASE,
        "episode_id": 141,
        "audio_id": 83,
        "audio_edit_url": "https://cms.test/cms/audio/audio/edit/83/",
        "audio_attached": True,
        "transcript_pending": False,
    }
    out = capsys.readouterr().out
    assert "Created draft episode 141 (python-315)." in out
    assert "Preview: https://cms.test/cms/pages/141/view_draft/" in out
    assert "Nothing was published." in out
    outputs = workspace / "auphonic" / "outputs"
    steps = out.split("Remaining manual steps in Wagtail, in this order:\n", 1)[1].splitlines()
    assert len(steps) == 4
    assert steps[0].startswith("  1. Add the further audio formats in https://cms.test/cms/audio/audio/edit/83/")
    assert f"M4A: {outputs / 'pp_73.m4a'}; OGA: {outputs / 'pp_73.ogg'}; OPUS: {outputs / 'pp_73.opus'}" in steps[0]
    assert steps[1].startswith(
        '  2. Set "Transcript diarization mode" to Enabled in https://cms.test/cms/audio/audio/edit/83/ and save'
    )
    assert steps[2].startswith(
        "  3. Add the contributors in the Contributors section of https://cms.test/cms/pages/141/edit/"
    )
    assert steps[2].endswith("jochen (host), dominik (host)")
    assert steps[3].startswith('  4. Then click "Generate transcript" in https://cms.test/cms/pages/141/edit/')
    # The future endpoints were only probed; nothing was sent to them.
    assert [request.method for request in fake.requests if request.url.path == "/api/editor/media/audios/83/"] == [
        "OPTIONS"
    ]
    assert [request.method for request in fake.requests if "transcript" in request.url.path] == ["OPTIONS"]


def test_rerun_patches_existing_draft_and_reuses_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _run(workspace)
    revision = fake.episodes[141]["latest_revision_id"]
    _write_selected(workspace, "title_seo.txt", "Neuer SEO-Titel\n")
    capsys.readouterr()

    _run(workspace)

    assert len(fake.audios) == 1
    assert len(fake.calls("POST", "/api/editor/episodes/")) == 1
    (patch,) = fake.calls("PATCH", "/api/editor/episodes/141/")
    body = json.loads(patch.content)
    assert body["base_revision_id"] == revision
    assert body["require_unpublished"] is True
    assert body["seo_title"] == "Neuer SEO-Titel"
    assert body["podcast_audio"] == {"id": 83}
    out = capsys.readouterr().out
    assert "Reusing Audio 83." in out
    assert "Updated draft episode 141 (python-315)." in out
    assert "pass --transcript to start it" in out
    # The Audio still lacks the formats and the mode, so the steps are listed again; the transcript was handed over.
    assert "1. Add the further audio formats" in out
    assert '2. Set "Transcript diarization mode" to Enabled' in out
    assert "3. Add the contributors" in out
    assert "Generate transcript" not in out


def test_rerun_after_slug_change_updates_stored_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _run(workspace)
    _write_selected(workspace, "slug.txt", "python-3-15\n")

    _run(workspace)

    assert len(fake.calls("POST", "/api/editor/episodes/")) == 1
    assert fake.episodes[141]["slug"] == "python-3-15"


def test_lookup_by_slug_finds_draft_without_stored_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _run(workspace)
    (workspace / "cms" / "draft.json").unlink()

    _run(workspace)

    assert len(fake.calls("POST", "/api/editor/episodes/")) == 1
    assert len(fake.audios) == 1
    (lookup, _) = fake.calls("GET", "/api/editor/posts/")
    assert dict(lookup.url.params) == {"parent": str(PODCAST_ID), "slug": "python-315"}
    assert len(fake.calls("PATCH", "/api/editor/episodes/141/")) == 1


def test_failed_create_keeps_uploaded_audio_for_the_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    fake.fail_episode_create = True
    with pytest.raises(typer.BadParameter, match=r"HTTP 400: validation_error\n  slug: Slug 'python-315'"):
        _run(workspace)
    fake.fail_episode_create = False

    capsys.readouterr()

    _run(workspace)

    assert len(fake.audios) == 1
    assert fake.episodes[141]["podcast_audio"] == {"id": 83}
    out = capsys.readouterr().out
    # The pending work of the Audio uploaded by the failed run is still listed.
    assert "1. Add the further audio formats" in out
    assert '4. Then click "Generate transcript"' in out


def test_new_audio_uploads_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _run(workspace)

    _run(workspace, new_audio=True)

    assert len(fake.audios) == 2
    assert fake.episodes[141]["podcast_audio"] == {"id": 84}
    assert fake.episodes[141]["overview"][1] == {"type": "audio", "value": {"id": 84}}
    assert "The previous Audio 83 is no longer used" in capsys.readouterr().out


def test_uses_future_endpoints_when_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    fake.accept_enabled_diarization = True
    fake.supports_contributors = True
    fake.options_allow = {
        "/api/editor/media/audios/83/": "GET, PATCH, OPTIONS",
        "/api/editor/episodes/141/transcript/": "POST, OPTIONS",
    }
    _run(workspace)

    assert fake.audios[83]["mode"] == "enabled"
    assert fake.audios[83]["added"] == [(b"m4a", b"pp_73.m4a"), (b"oga", b"pp_73.ogg"), (b"opus", b"pp_73.opus")]
    assert len(fake.calls("PATCH", "/api/editor/media/audios/83/")) == 3
    (contributors_patch,) = fake.calls("PATCH", "/api/editor/episodes/141/")
    assert json.loads(contributors_patch.content)["contributors"] == [
        {"slug": "jochen", "role": "host"},
        {"slug": "dominik", "role": "host"},
    ]
    assert fake.episodes[141]["contributors"] == [
        {"slug": "jochen", "role": "host"},
        {"slug": "dominik", "role": "host"},
    ]
    assert len(fake.calls("POST", "/api/editor/episodes/141/transcript/")) == 1
    out = capsys.readouterr().out
    assert "Set contributors: jochen (host), dominik (host)." in out
    assert "Transcript generation enqueued" in out
    assert "No manual steps remain." in out


def test_sets_diarization_after_upload_when_audio_patch_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path, extra_formats=False)
    _config(tmp_path, monkeypatch)
    fake.supports_contributors = True
    fake.options_allow = {
        "/api/editor/media/audios/83/": "PATCH",
        "/api/editor/episodes/141/transcript/": "POST",
    }
    _run(workspace)

    (mode_patch,) = fake.calls("PATCH", "/api/editor/media/audios/83/")
    assert parse_qs(mode_patch.read().decode()) == {"transcript_diarization_mode": ["enabled"]}
    assert fake.audios[83]["mode"] == "enabled"
    order = [request.url.path for request in fake.requests if request.method in {"POST", "PATCH"}]
    assert order.index("/api/editor/media/audios/83/") < order.index("/api/editor/episodes/141/transcript/")
    assert "No manual steps remain." in capsys.readouterr().out


def test_rerun_reads_diarization_changed_in_wagtail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path, extra_formats=False)
    _config(tmp_path, monkeypatch)
    _run(workspace)
    fake.audios[83]["mode"] = "enabled"  # set by hand in the Wagtail audio editor
    fake.supports_contributors = True  # django-cast gained contributors meanwhile
    fake.options_allow = {"/api/editor/episodes/141/transcript/": "POST"}
    capsys.readouterr()

    _run(workspace, transcript=True)

    out = capsys.readouterr().out
    assert "Transcript diarization mode" not in out
    assert len(fake.calls("POST", "/api/editor/episodes/141/transcript/")) == 1
    assert "No manual steps remain." in out


def test_contributors_already_set_are_not_patched_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    fake.supports_contributors = True
    _run(workspace)
    patches = len(fake.calls("PATCH", "/api/editor/episodes/141/"))

    _run(workspace)

    (update,) = fake.calls("PATCH", "/api/editor/episodes/141/")[patches:]
    assert "contributors" not in json.loads(update.content)


def test_host_without_contributor_mapping_is_a_manual_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch, contributors={"Jochen": "jochen"})
    fake.accept_enabled_diarization = True
    fake.supports_contributors = True
    fake.options_allow = {"/api/editor/media/audios/83/": "PATCH", "/api/editor/episodes/141/transcript/": "POST"}
    _run(workspace)

    assert fake.episodes[141]["contributors"] == [{"slug": "jochen", "role": "host"}]
    out = capsys.readouterr().out
    assert "1. Credit Dominik in the Contributors section of https://cms.test/cms/pages/141/edit/" in out
    assert '2. Then re-run `podcast cms-draft`, or click "Generate transcript"' in out
    assert fake.calls("POST", "/api/editor/episodes/141/transcript/") == []
    assert json.loads((workspace / "cms" / "draft.json").read_text(encoding="utf-8"))["transcript_pending"] is True


def test_refuses_to_change_live_episode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _run(workspace)
    fake.episodes[141].update(live=True, status="live")

    with pytest.raises(typer.BadParameter, match="already published"):
        _run(workspace)
    assert fake.calls("PATCH", "/api/editor/episodes/141/") == []


def test_slug_taken_by_plain_post_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    fake.other_posts["python-315"] = {"id": 7, "type": "cast.Post", "slug": "python-315", "edit_url": "/cms/7/"}
    with pytest.raises(typer.BadParameter, match="already used by a cast.Post page"):
        _run(workspace)
    assert fake.audios == {}


def test_parent_must_be_an_accessible_podcast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch, parent_id=2)
    with pytest.raises(typer.BadParameter, match=r"cms.parent_id 2 is not a podcast .*120: Podcast"):
        _run(workspace)


def test_parent_is_resolved_when_only_one_podcast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch, parent_id=None)
    _run(workspace)
    assert fake.episodes[141]["parent"] == {"id": PODCAST_ID}


def test_redirects_are_not_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    fake.redirect_all = True
    with pytest.raises(typer.BadParameter, match="redirected to https://elsewhere.test/api/"):
        _run(workspace)
    assert {request.url.host for request in fake.requests} == {"cms.test"}


def test_missing_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    monkeypatch.delenv("TEST_CMS_TOKEN")
    with pytest.raises(typer.BadParameter, match="set TEST_CMS_TOKEN"):
        _run(workspace)
    assert fake.requests == []


def test_revision_conflict_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "revision_conflict", "current_revision_id": 9})

    with DjangoCastClient(BASE, TOKEN, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CmsApiError, match="draft changed since it was read"):
            client.update_episode(1, {"title": "x"}, base_revision_id=8)


def test_client_rejects_http_base_url() -> None:
    with pytest.raises(CmsConfigError):
        DjangoCastClient("http://cms.test/", TOKEN)


def _publish_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast) -> Path:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _run(workspace)
    return workspace


def test_publish_asks_for_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast) -> None:
    workspace = _publish_setup(tmp_path, monkeypatch, fake)
    result = CliRunner().invoke(app, ["cms-publish", "--workspace", str(workspace)], input="n\n")
    assert result.exit_code == 1
    assert "Publish this episode on the live site now?" in result.output
    assert "Not published." in result.output
    assert fake.calls("POST", "/api/editor/episodes/141/publish/") == []


def test_publish_with_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast) -> None:
    workspace = _publish_setup(tmp_path, monkeypatch, fake)
    result = CliRunner().invoke(app, ["cms-publish", "--workspace", str(workspace), "--yes"])
    assert result.exit_code == 0, result.output
    assert len(fake.calls("POST", "/api/editor/episodes/141/publish/")) == 1
    assert "Published episode 141: https://cms.test/podcast/python-315/" in result.output

    again = CliRunner().invoke(app, ["cms-publish", "--workspace", str(workspace), "--yes"])
    assert again.exit_code != 0
    assert len(fake.calls("POST", "/api/editor/episodes/141/publish/")) == 1


def test_cli_cms_draft_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    result = CliRunner().invoke(app, ["cms-draft", "--workspace", str(workspace), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["lookup"]["params"] == {"parent": PODCAST_ID, "slug": "python-315"}


def _all_endpoints(fake: FakeCast) -> None:
    fake.accept_enabled_diarization = True
    fake.supports_contributors = True
    fake.options_allow = {"/api/editor/media/audios/83/": "PATCH", "/api/editor/episodes/141/transcript/": "POST"}


def _set_episode_yaml(workspace: Path, **changes: Any) -> None:
    path = workspace / "episode.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_no_listed_hosts_blocks_the_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _set_episode_yaml(workspace, hosts=None)
    _config(tmp_path, monkeypatch)
    _all_endpoints(fake)
    _run(workspace)

    out = capsys.readouterr().out
    assert "1. Add the episode's speakers in the Contributors section" in out
    assert "episode.yaml lists no hosts" in out
    assert fake.calls("POST", "/api/editor/episodes/141/transcript/") == []


@pytest.mark.parametrize("mode", ["inherit", "disabled"])
def test_transcript_needs_confirmed_enabled_diarization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    workspace = _workspace(tmp_path, extra_formats=False)
    _config(tmp_path, monkeypatch, transcript_diarization=mode)
    _all_endpoints(fake)
    _run(workspace)

    assert fake.audios[83]["mode"] == mode
    assert fake.calls("POST", "/api/editor/episodes/141/transcript/") == []
    assert "speaker labels need diarization mode Enabled" in capsys.readouterr().out


def test_unknown_audio_is_checked_by_hand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _all_endpoints(fake)
    _run(workspace)
    fake.hidden_audios.add(83)  # no longer in the editor audio list
    capsys.readouterr()

    _run(workspace, transcript=True)

    out = capsys.readouterr().out
    assert "Audio 83 is not in the editor audio list" in out
    assert "1. Check that https://cms.test/cms/audio/audio/edit/83/ has the further audio formats" in out
    assert '2. Check that "Transcript diarization mode" is Enabled' in out
    assert len(fake.calls("PATCH", "/api/editor/media/audios/83/")) == 3  # only the first run's format uploads
    assert len(fake.calls("POST", "/api/editor/episodes/141/transcript/")) == 1  # only the first run's


def test_read_only_contributors_are_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path, extra_formats=False)
    _config(tmp_path, monkeypatch)
    _all_endpoints(fake)
    fake.contributors_read_only = True
    _run(workspace)

    out = capsys.readouterr().out
    assert "the editor API did not store them; it returned none" in out
    assert fake.calls("POST", "/api/editor/episodes/141/transcript/") == []


def test_failed_attach_of_new_audio_is_retried_with_that_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _run(workspace)
    fake.fail_episode_patch = True
    capsys.readouterr()
    with pytest.raises(typer.BadParameter, match="page_locked"):
        _run(workspace, new_audio=True)
    assert "no longer used" not in capsys.readouterr().out
    assert fake.episodes[141]["podcast_audio"] == {"id": 83}
    fake.fail_episode_patch = False

    _run(workspace)

    assert sorted(fake.audios) == [83, 84]
    assert fake.episodes[141]["podcast_audio"] == {"id": 84}
    out = capsys.readouterr().out
    assert "Reusing Audio 84." in out
    assert "The previous Audio 83 is no longer used by the draft; delete it in Wagtail." in out
    assert '4. Then click "Generate transcript"' in out


def test_find_audio_pages_through_the_list_without_following_next_links() -> None:
    fake = FakeCast()
    fake.list_page_size_cap = 2
    for audio_id in range(1, 6):
        fake.audios[audio_id] = {"file": (b"mp3", b"a.mp3"), "fields": {}, "mode": "inherit"}
    with DjangoCastClient(BASE, TOKEN, transport=fake.transport()) as client:
        assert client.find_audio(1) is not None
        assert client.find_audio(99) is None
    assert {request.url.host for request in fake.requests} == {"cms.test"}


def test_diarization_patch_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path, extra_formats=False)
    _config(tmp_path, monkeypatch)
    _all_endpoints(fake)
    fake.accept_enabled_diarization = False
    fake.diarization_read_only = True
    _run(workspace)

    assert fake.audios[83]["mode"] == "inherit"
    out = capsys.readouterr().out
    assert "the editor API did not store it; Audio 83 reports 'inherit'" in out
    assert fake.calls("POST", "/api/editor/episodes/141/transcript/") == []
