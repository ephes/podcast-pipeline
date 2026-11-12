"""Synthetic CMS preview, privacy, path and publication-boundary regressions."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
import yaml

from podcast_pipeline.cms_api import CmsApiError, DjangoCastClient
from podcast_pipeline.entrypoints import cms_draft as entry
from tests.test_cms_draft import BASE, PODCAST_ID, TOKEN, FakeCast, _config, _run, _workspace, _write_selected


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeCast:
    server = FakeCast()
    monkeypatch.setattr(
        entry,
        "DjangoCastClient",
        lambda base_url, token: DjangoCastClient(base_url, token, transport=server.transport()),
    )
    return server


def test_dry_run_reports_options_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("dry run attempted token lookup, client creation or locking")

    monkeypatch.setattr(entry, "load_cms_token", forbidden)
    monkeypatch.setattr(entry, "DjangoCastClient", forbidden)
    monkeypatch.setattr(entry, "_cms_lock", forbidden)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    plans = []
    for options in [{}, {"new_audio": True}, {"transcript": True}]:
        entry.run_cms_draft(workspace=workspace, dry_run=True, **options)
        plans.append(json.loads(capsys.readouterr().out))
    assert plans[0] != plans[1] != plans[2] != plans[0]
    assert plans[1]["intent"]["audio"] == "force_upload"
    assert plans[0]["intent"]["audio"] == "reuse_or_upload"
    assert plans[2]["intent"]["transcript"] == "requested"
    assert "unresolved" in plans[0]["intent"]["remote_decisions"]
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_dry_run_shows_bound_local_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    path = workspace / "cms/draft.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "base_url": BASE,
                "parent_id": PODCAST_ID,
                "audio_id": 83,
                "audio_attached": False,
                "transcript_pending": True,
            }
        )
    )
    before = path.read_bytes()
    entry.run_cms_draft(workspace=workspace, dry_run=True)
    plan = json.loads(capsys.readouterr().out)
    assert plan["intent"]["local_audio_id"] == 83
    assert plan["intent"]["transcript"] == "pending"
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["redirect", "plain", "json", "conflict", "validation"])
def test_untrusted_errors_hide_private_payloads(kind: str) -> None:
    calls = []
    secret = "SIGNED_PRIVATE_SENTINEL"

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if kind == "redirect":
            return httpx.Response(
                302, headers={"Location": f"https://{secret}:password@other.test/api/?token={secret}#{secret}"}
            )
        if kind == "plain":
            return httpx.Response(500, text=secret)
        if kind == "conflict":
            return httpx.Response(409, json={"code": "revision_conflict", "current_revision_id": secret})
        if kind == "validation":
            return httpx.Response(
                400,
                json={
                    "code": "validation_error",
                    "errors": {"slug": [{"code": "invalid", "message": secret}], secret: secret},
                },
            )
        return httpx.Response(400, json={"code": secret, "detail": secret})

    client = DjangoCastClient(BASE, TOKEN, transport=httpx.MockTransport(handle))
    with pytest.raises(CmsApiError) as exc:
        client.create_episode({})
    assert secret not in str(exc.value)
    assert "POST" in str(exc.value) and "api/editor/episodes/" in str(exc.value)
    if kind != "redirect":
        assert "HTTP" in str(exc.value)
    if kind == "conflict":
        assert "draft changed" in str(exc.value)
    if kind == "validation":
        assert "slug: invalid" in str(exc.value)
    assert len(calls) == 1


@pytest.mark.parametrize("key", ["episode_id", "output_basename"])
@pytest.mark.parametrize("value", ["../outside", "/tmp/outside", "..\\outside"])
def test_implicit_basename_stays_inside_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, key: str, value: str
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    path = workspace / "episode.yaml"
    data = yaml.safe_load(path.read_text())
    if key == "episode_id":
        data[key] = value
    else:
        data["auphonic"][key] = value
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(typer.BadParameter, match="file name without directories"):
        _run(workspace)
    assert not fake.requests


@pytest.mark.parametrize("suffix", ["mp3", "m4a"])
def test_implicit_symlink_escape_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, suffix: str
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    outside = tmp_path / f"outside.{suffix}"
    outside.write_bytes(b"synthetic")
    link = workspace / f"auphonic/outputs/pp_73.{suffix}"
    link.unlink()
    link.symlink_to(outside)
    with pytest.raises(typer.BadParameter, match="outside.*outputs"):
        _run(workspace)
    assert not fake.requests


def test_explicit_audio_can_be_outside_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    path = workspace / "episode.yaml"
    data = yaml.safe_load(path.read_text())
    outside = tmp_path / "explicit.mp3"
    outside.write_bytes(b"synthetic")
    data["cms"] = {"audio_file": str(outside)}
    data["auphonic"]["output_basename"] = "../unused"
    path.write_text(yaml.safe_dump(data))
    _run(workspace)
    assert len(fake.episodes) == 1


@pytest.mark.parametrize("change", ["description", "slug", "config", "invalid_slug", "heading_only", "invalid_config"])
@pytest.mark.parametrize("existing", [False, True])
def test_copy_or_config_change_during_audio_setup_stops_episode_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, change: str, existing: bool
) -> None:
    workspace = _workspace(tmp_path)
    config_path = _config(tmp_path, monkeypatch)
    if existing:
        _run(workspace)
        fake.requests.clear()
    original = fake._upload
    changed = []

    def upload(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if response.status_code == 201:
            changed.append(True)
            if change in {"config", "invalid_config"}:
                data = yaml.safe_load(config_path.read_text())
                data["cms"]["detail_intro_html"] = "<p>New intro</p>" if change == "config" else None
                config_path.write_text(yaml.safe_dump(data))
            else:
                name = "slug.txt" if "slug" in change else "description.md"
                if change == "heading_only":
                    path = workspace / "copy/selected/description.md"
                    _write_selected(workspace, name, path.read_text().replace("Python 3.15 ist da!", "New heading"))
                else:
                    _write_selected(workspace, name, "Bad Slug!" if change == "invalid_slug" else "changed-selection")
        return response

    monkeypatch.setattr(fake, "_upload", upload)
    with pytest.raises(typer.BadParameter, match="inputs changed.*retry"):
        _run(workspace, new_audio=True)
    assert changed == [True]
    assert not [
        r for r in fake.requests if r.url.path.startswith("/api/editor/episodes/") and r.method in {"POST", "PATCH"}
    ]
    state = json.loads((workspace / "cms/draft.json").read_text())
    assert state["audio_id"] == (84 if existing else 83)
    assert state["audio_attached"] is False


def test_plan_loaded_under_cms_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)

    @contextmanager
    def ownership(*args: Any) -> Iterator[None]:
        _write_selected(workspace, "slug.txt", "selected-after-lock")
        yield

    monkeypatch.setattr(entry, "_cms_lock", ownership)
    _run(workspace)
    assert fake.episodes[141]["slug"] == "selected-after-lock"


def test_transport_error_omits_private_exception_text() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("SIGNED_PRIVATE_SENTINEL", request=request)

    client = DjangoCastClient(BASE, TOKEN, transport=httpx.MockTransport(fail))
    with pytest.raises(CmsApiError, match="transport error") as exc:
        client.list_parents()
    assert "SIGNED_PRIVATE_SENTINEL" not in str(exc.value)


@pytest.mark.parametrize("endpoint", ["upload", "create"])
def test_malformed_remote_success_stops_followup_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, endpoint: str
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    original = fake._upload if endpoint == "upload" else fake._create

    def malformed(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if response.status_code == 201:
            return httpx.Response(201, json={"id": True, "private": "SIGNED_PRIVATE_SENTINEL"})
        return response

    monkeypatch.setattr(fake, "_upload" if endpoint == "upload" else "_create", malformed)
    with pytest.raises(typer.BadParameter, match="invalid id") as exc:
        _run(workspace)
    assert "SIGNED_PRIVATE_SENTINEL" not in str(exc.value)
    assert len(fake.audios) == 1
    assert len(fake.episodes) == (0 if endpoint == "upload" else 1)
    assert not [r for r in fake.requests if r.method == "PATCH"]
