"""Production history in state.json and cleanup of superseded Auphonic productions.

Deleting a production is irreversible, so these tests pin down what may be
deleted: only superseded productions from the workspace's own history, never
the current one, and only after the current one is Done with downloaded
outputs. Every request goes through ``httpx.MockTransport``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import typer
from typer.testing import CliRunner

from podcast_pipeline import auphonic_api
from podcast_pipeline.auphonic_api import AuphonicApiError, AuphonicClient, AuphonicCredentials, AuphonicNotFoundError
from podcast_pipeline.auphonic_history import plan_cleanup
from podcast_pipeline.domain.models import AuphonicProductionRecord, AuphonicProductionStatus, EpisodeWorkspace
from podcast_pipeline.entrypoints import produce
from podcast_pipeline.entrypoints.cli import app
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore
from tests.test_auphonic_api import CREDS, _client, _FakeAuphonic, _ok, _Recorder, _workspace

DONE = (3, "Done")
SUPERSEDED = AuphonicProductionStatus.superseded
CURRENT = AuphonicProductionStatus.current
DELETED = AuphonicProductionStatus.deleted


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auphonic_api.time, "sleep", lambda _seconds: None)


@pytest.fixture()
def fake_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Callable[[list[tuple[int, str]]], _FakeAuphonic]]:
    holder: dict[str, _FakeAuphonic] = {}

    def install(script: list[tuple[int, str]]) -> _FakeAuphonic:
        holder["fake"] = _FakeAuphonic(script)
        return holder["fake"]

    def make_client(credentials: AuphonicCredentials) -> AuphonicClient:
        return AuphonicClient(credentials, transport=httpx.MockTransport(holder["fake"].recorder))

    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(tmp_path / "missing-config.yaml"))
    monkeypatch.setattr(produce, "AuphonicClient", make_client)
    monkeypatch.setattr(produce, "load_auphonic_credentials", lambda: CREDS)
    monkeypatch.setattr(
        produce,
        "build_auphonic_payload",
        lambda **_kwargs: {
            "preset": "p1",
            "is_multitrack": True,
            "multi_input_files": [{"type": "multitrack", "id": "jochen", "input_file": "https://cdn.example/j.flac"}],
        },
    )
    yield install


def _record(uuid: str, status: AuphonicProductionStatus, **extra: object) -> AuphonicProductionRecord:
    return AuphonicProductionRecord.model_validate({"uuid": uuid, "status": status, **extra})


def _state_with_history(store: EpisodeWorkspaceStore, *records: AuphonicProductionRecord) -> None:
    current = next((record.uuid for record in records if record.status == CURRENT), None)
    store.write_state(
        EpisodeWorkspace(
            episode_id="ep_001",
            root_dir=".",
            auphonic_production_uuid=current,
            auphonic_productions=list(records),
        )
    )


def _downloaded(uuid: str) -> AuphonicProductionRecord:
    return _record(uuid, CURRENT, last_known_state="done", outputs_downloaded_at=datetime.now(UTC))


def _deletes(fake: _FakeAuphonic) -> list[str]:
    return [path for method, path in fake.recorder.calls if method == "DELETE"]


# --- history ------------------------------------------------------------------


def test_restart_keeps_the_discarded_production_in_the_history(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([(4, "Audio Processing"), (2, "Error")])
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    with pytest.raises(typer.BadParameter, match="prod-1 failed"):
        produce.run_produce(workspace=workspace, dry_run=False)

    fake.script = [DONE]
    fake.fixed_status["prod-1"] = (2, "Error")
    produce.run_produce(workspace=workspace, dry_run=False, restart=True)

    state = store.read_state()
    assert state.auphonic_production_uuid == "prod-2"
    old, new = state.auphonic_productions
    assert (old.uuid, old.status, old.last_known_state) == ("prod-1", SUPERSEDED, "error")
    assert old.superseded_by == "prod-2"
    assert old.superseded_reason == "replaced by `podcast produce --restart`"
    assert old.created_at is not None and old.superseded_at is not None
    assert (new.uuid, new.status, new.last_known_state) == ("prod-2", CURRENT, "done")
    assert new.outputs_downloaded_at is not None
    # Cleanup is opt-in: nothing was deleted.
    assert _deletes(fake) == []


def test_a_stored_uuid_from_before_the_history_is_recorded_on_restart(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([DONE])
    fake.fixed_status["prod-legacy"] = (10, "Production Not Started Yet")
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    (workspace / "state.json").write_text(
        EpisodeWorkspace(episode_id="ep_001", root_dir=".", auphonic_production_uuid="prod-legacy").to_json(),
        encoding="utf-8",
    )

    produce.run_produce(workspace=workspace, dry_run=False, restart=True)

    legacy, new = store.read_state().auphonic_productions
    assert (legacy.uuid, legacy.status, legacy.last_known_state) == ("prod-legacy", SUPERSEDED, "not_started")
    assert legacy.created_at is None
    assert (new.uuid, new.status) == ("prod-1", CURRENT)


def test_stale_writers_do_not_drop_the_history(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(_workspace(tmp_path))
    stale = EpisodeWorkspace(episode_id="ep_001", root_dir=".")
    store.set_auphonic_production_uuid("prod-1", default=stale)
    store.set_auphonic_production_uuid("prod-2", default=stale, superseded_reason="test")

    store.write_state(stale)

    state = store.read_state()
    assert state.auphonic_production_uuid == "prod-2"
    assert [(record.uuid, record.status) for record in state.auphonic_productions] == [
        ("prod-1", SUPERSEDED),
        ("prod-2", CURRENT),
    ]


def test_state_without_history_keeps_its_json_shape(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(_workspace(tmp_path))
    store.write_state(EpisodeWorkspace(episode_id="ep_001", root_dir="."))
    assert "auphonic_productions" not in store.layout.state_json.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "records",
    [
        [_record("a", CURRENT), _record("a", SUPERSEDED)],
        [_record("a", CURRENT), _record("b", CURRENT)],
    ],
)
def test_history_rejects_duplicate_or_ambiguous_entries(records: list[AuphonicProductionRecord]) -> None:
    with pytest.raises(ValueError):
        EpisodeWorkspace(episode_id="ep", root_dir=".", auphonic_production_uuid="a", auphonic_productions=records)


def test_the_stored_production_can_never_be_superseded_in_the_history() -> None:
    with pytest.raises(ValueError, match="must not be superseded"):
        EpisodeWorkspace(
            episode_id="ep",
            root_dir=".",
            auphonic_production_uuid="a",
            auphonic_productions=[_record("a", SUPERSEDED)],
        )
    with pytest.raises(ValueError, match="must match"):
        EpisodeWorkspace(
            episode_id="ep",
            root_dir=".",
            auphonic_production_uuid="b",
            auphonic_productions=[_record("a", CURRENT)],
        )


def test_record_uuids_must_be_url_safe() -> None:
    with pytest.raises(ValueError):
        _record("../productions", SUPERSEDED)


# --- cleanup after produce --------------------------------------------------------


def test_cleanup_after_restart_deletes_only_the_superseded_production(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic],
) -> None:
    fake = fake_api([DONE])
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    produce.run_produce(workspace=workspace, dry_run=False)
    fake.fixed_status["prod-1"] = DONE
    capsys.readouterr()

    produce.run_produce(workspace=workspace, dry_run=False, restart=True, cleanup=True)

    out = capsys.readouterr().out
    assert fake.deleted == ["prod-1"]
    assert _deletes(fake) == ["/api/production/prod-1.json"]
    assert "Deleting superseded Auphonic production prod-1" in out
    assert "Deleted Auphonic production prod-1." in out
    state = store.read_state()
    assert state.auphonic_production_uuid == "prod-2"
    old, new = state.auphonic_productions
    assert (old.uuid, old.status, old.last_known_state) == ("prod-1", DELETED, "done")
    assert old.deleted_at is not None
    assert new.status == CURRENT
    # The cleanup ran after the new production's outputs were downloaded.
    first_delete = next(i for i, (method, _path) in enumerate(fake.recorder.calls) if method == "DELETE")
    downloads = [i for i, (_method, path) in enumerate(fake.recorder.calls) if path.startswith("/download/prod-2")]
    assert downloads and max(downloads) < first_delete


def test_cleanup_is_enabled_by_episode_yaml_and_disabled_by_no_cleanup(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([DONE])
    workspace = _workspace(tmp_path)
    (workspace / "episode.yaml").write_text("episode_id: ep_001\nauphonic:\n  cleanup_superseded: true\n")
    produce.run_produce(workspace=workspace, dry_run=False)
    fake.fixed_status["prod-1"] = DONE

    produce.run_produce(workspace=workspace, dry_run=False, restart=True, cleanup=False)
    assert fake.deleted == []

    produce.run_produce(workspace=workspace, dry_run=False, restart=True)
    assert fake.deleted == ["prod-1", "prod-2"]


def test_cleanup_is_enabled_by_the_global_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic],
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("auphonic:\n  cleanup_superseded: true\n", encoding="utf-8")
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(config))
    fake = fake_api([DONE])
    workspace = _workspace(tmp_path)
    produce.run_produce(workspace=workspace, dry_run=False)
    fake.fixed_status["prod-1"] = DONE

    produce.run_produce(workspace=workspace, dry_run=False, restart=True)

    assert fake.deleted == ["prod-1"]


def test_invalid_cleanup_setting_fails_before_any_request(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([DONE])
    workspace = _workspace(tmp_path)
    (workspace / "episode.yaml").write_text("episode_id: ep_001\nauphonic:\n  cleanup_superseded: 'yes'\n")

    with pytest.raises(typer.BadParameter, match="cleanup_superseded must be true or false"):
        produce.run_produce(workspace=workspace, dry_run=False)
    assert fake.recorder.requests == []


def test_no_cleanup_when_the_new_production_fails(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([DONE])
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    produce.run_produce(workspace=workspace, dry_run=False)
    fake.fixed_status["prod-1"] = DONE
    fake.script = [(4, "Audio Processing"), (2, "Error")]

    with pytest.raises(typer.BadParameter, match="prod-2 failed"):
        produce.run_produce(workspace=workspace, dry_run=False, restart=True, cleanup=True)

    assert _deletes(fake) == []
    old, new = store.read_state().auphonic_productions
    assert (old.uuid, old.status) == ("prod-1", SUPERSEDED)
    assert (new.uuid, new.outputs_downloaded_at) == ("prod-2", None)
    # The standalone cleanup refuses for the same reason.
    with pytest.raises(typer.BadParameter, match="have not been downloaded"):
        produce.run_auphonic_cleanup(workspace=workspace, dry_run=False)
    assert _deletes(fake) == []


def test_no_cleanup_when_the_download_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic],
) -> None:
    fake = fake_api([DONE])
    workspace = _workspace(tmp_path)
    produce.run_produce(workspace=workspace, dry_run=False)
    fake.fixed_status["prod-1"] = DONE

    def failing_download(*_args: object, **_kwargs: object) -> None:
        raise AuphonicApiError("disk full")

    monkeypatch.setattr(AuphonicClient, "download_outputs", failing_download)
    with pytest.raises(typer.BadParameter, match="disk full"):
        produce.run_produce(workspace=workspace, dry_run=False, restart=True, cleanup=True)

    assert _deletes(fake) == []


def test_update_forgets_the_downloaded_outputs_until_the_rerun_is_downloaded(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([DONE])
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    produce.run_produce(workspace=workspace, dry_run=False)
    produce.run_produce(workspace=workspace, dry_run=False, restart=True)
    fake.script = [(4, "Audio Processing"), (2, "Error")]

    with pytest.raises(typer.BadParameter, match="prod-2 failed"):
        produce.run_produce(workspace=workspace, dry_run=False, update=True)
    current = store.read_state().auphonic_productions[-1]
    assert (current.uuid, current.outputs_downloaded_at, current.last_known_state) == ("prod-2", None, None)
    with pytest.raises(typer.BadParameter, match="have not been downloaded"):
        produce.run_auphonic_cleanup(workspace=workspace, dry_run=False)

    fake.script = [(4, "Audio Processing"), DONE]
    produce.run_produce(workspace=workspace, dry_run=False, update=True, cleanup=True)
    assert fake.deleted == ["prod-1"]


def test_auto_cleanup_problems_are_warnings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic],
) -> None:
    fake = fake_api([DONE])
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    produce.run_produce(workspace=workspace, dry_run=False)
    fake.fixed_status["prod-1"] = DONE
    fake.delete_status["prod-1"] = 500
    capsys.readouterr()

    produce.run_produce(workspace=workspace, dry_run=False, restart=True, cleanup=True)

    captured = capsys.readouterr()
    assert "Auphonic production complete: prod-2" in captured.out
    assert "Could not delete Auphonic production prod-1" in captured.err
    assert "auphonic-cleanup --delete" in captured.err
    assert store.read_state().auphonic_productions[0].status == SUPERSEDED


# --- podcast auphonic-cleanup ----------------------------------------------------


def test_dry_run_lists_each_uuid_without_credentials_or_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic],
) -> None:
    fake = fake_api([DONE])
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    _state_with_history(
        store,
        _record("old-a", SUPERSEDED, superseded_reason="replaced by `podcast produce --restart`", superseded_by="cur"),
        _record("old-b", SUPERSEDED, last_known_state="not_started"),
        _record("gone", DELETED),
        _downloaded("cur"),
    )

    def no_credentials() -> AuphonicCredentials:
        raise AssertionError("dry run must not load credentials")

    monkeypatch.setattr(produce, "load_auphonic_credentials", no_credentials)
    result = CliRunner().invoke(app, ["auphonic-cleanup", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "would delete 2 superseded Auphonic production(s)" in result.output
    assert "current production: cur, kept" in result.output
    assert "old-a (superseded: replaced by `podcast produce --restart`; by cur)" in result.output
    assert "old-b (last seen not_started)" in result.output
    assert "gone" not in result.output
    assert "--delete" in result.output
    assert fake.recorder.requests == []


def test_dry_run_says_when_nothing_would_be_deleted_yet(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    _state_with_history(store, _record("old", SUPERSEDED), _record("cur", CURRENT))

    result = CliRunner().invoke(app, ["auphonic-cleanup", "--workspace", str(workspace), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "old" in result.output
    assert "Nothing would be deleted right now" in result.output


def test_delete_removes_only_superseded_history_entries(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([DONE])
    for uuid in ("cur", "old-a", "old-b", "gone", "unrelated"):
        fake.fixed_status[uuid] = DONE
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    _state_with_history(
        store,
        _record("old-a", SUPERSEDED),
        _record("old-b", SUPERSEDED),
        _record("gone", DELETED),
        _downloaded("cur"),
    )

    result = CliRunner().invoke(app, ["auphonic-cleanup", "--workspace", str(workspace), "--delete"])

    assert result.exit_code == 0, result.output
    assert fake.deleted == ["old-a", "old-b"]
    assert "Deleting superseded Auphonic production old-a" in result.output
    assert "Deleting superseded Auphonic production old-b" in result.output
    state = store.read_state()
    assert [(record.uuid, record.status) for record in state.auphonic_productions] == [
        ("old-a", DELETED),
        ("old-b", DELETED),
        ("gone", DELETED),
        ("cur", CURRENT),
    ]

    # A second run has nothing left to do.
    result = CliRunner().invoke(app, ["auphonic-cleanup", "--workspace", str(workspace), "--delete"])
    assert result.exit_code == 0, result.output
    assert "No superseded Auphonic productions to delete." in result.output
    assert fake.deleted == ["old-a", "old-b"]


@pytest.mark.parametrize(
    ("status", "detail"),
    [((15, "Production Changed"), "Production Changed"), ((4, "Audio Processing"), "Audio Processing")],
)
def test_delete_refuses_while_the_current_production_is_not_done(
    tmp_path: Path,
    fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic],
    status: tuple[int, str],
    detail: str,
) -> None:
    fake = fake_api([DONE])
    fake.fixed_status["cur"] = status
    workspace = _workspace(tmp_path)
    _state_with_history(EpisodeWorkspaceStore(workspace), _record("old", SUPERSEDED), _downloaded("cur"))

    with pytest.raises(typer.BadParameter, match=f"current production cur is not Done.*{detail}"):
        produce.run_auphonic_cleanup(workspace=workspace, dry_run=False)

    assert _deletes(fake) == []


def test_delete_refuses_without_a_current_production(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([DONE])
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    store.write_state(
        EpisodeWorkspace(episode_id="ep_001", root_dir=".", auphonic_productions=[_record("old", SUPERSEDED)])
    )

    with pytest.raises(typer.BadParameter, match="no current Auphonic production"):
        produce.run_auphonic_cleanup(workspace=workspace, dry_run=False)

    assert fake.recorder.requests == []


def test_delete_skips_running_records_missing_and_reports_failures(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([DONE])
    fake.fixed_status.update({"cur": DONE, "running": (4, "Audio Processing"), "broken": (2, "Error")})
    fake.missing.add("vanished")
    fake.delete_status["broken"] = 403
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    _state_with_history(
        store,
        _record("running", SUPERSEDED),
        _record("vanished", SUPERSEDED),
        _record("broken", SUPERSEDED),
        _downloaded("cur"),
    )

    with pytest.raises(typer.BadParameter, match="superseded Auphonic productions broken"):
        produce.run_auphonic_cleanup(workspace=workspace, dry_run=False)

    assert _deletes(fake) == ["/api/production/broken.json"]
    records = {record.uuid: record for record in store.read_state().auphonic_productions}
    assert (records["running"].status, records["running"].last_known_state) == (SUPERSEDED, "running")
    assert (records["vanished"].status, records["vanished"].last_known_state) == (DELETED, "missing")
    assert records["broken"].status == SUPERSEDED
    assert records["cur"].status == CURRENT


def test_cleanup_without_state_json_is_a_no_op(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["auphonic-cleanup", "--workspace", str(_workspace(tmp_path)), "--delete"])
    assert result.exit_code == 0, result.output
    assert "no Auphonic productions are recorded" in result.output


def test_plan_never_includes_the_current_production() -> None:
    state = EpisodeWorkspace(
        episode_id="ep",
        root_dir=".",
        auphonic_production_uuid="cur",
        auphonic_productions=[_record("old", SUPERSEDED), _downloaded("cur")],
    )
    plan = plan_cleanup(state)
    assert [record.uuid for record in plan.candidates] == ["old"]
    assert plan.blocked_reason is None


# --- AuphonicClient.delete_production --------------------------------------------


def test_delete_production_sends_an_authenticated_delete() -> None:
    recorder = _Recorder(lambda _request: _ok({}))
    with _client(recorder) as client:
        client.delete_production("abc123")
    assert recorder.calls == [("DELETE", "/api/production/abc123.json")]
    assert recorder.requests[0].headers["Authorization"] == "Bearer key"


def test_delete_production_accepts_an_empty_response() -> None:
    recorder = _Recorder(lambda _request: httpx.Response(204))
    with _client(recorder) as client:
        client.delete_production("abc123")
    assert recorder.calls == [("DELETE", "/api/production/abc123.json")]


def test_delete_production_reports_a_missing_production() -> None:
    recorder = _Recorder(lambda _request: httpx.Response(404, json={"error_message": "not found"}))
    with _client(recorder) as client, pytest.raises(AuphonicNotFoundError):
        client.delete_production("abc123")


def test_delete_production_refuses_a_uuid_that_is_not_url_safe() -> None:
    recorder = _Recorder(lambda _request: _ok({}))
    with _client(recorder) as client, pytest.raises(AuphonicApiError, match="invalid UUID"):
        client.delete_production("../productions")
    assert recorder.requests == []


# --- productions deleted in the Auphonic web UI (404) ----------------------------


def _deleted_in_ui(tmp_path: Path, fake: _FakeAuphonic) -> tuple[Path, EpisodeWorkspaceStore]:
    """A finished production that was then deleted by hand in Auphonic."""
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    produce.run_produce(workspace=workspace, dry_run=False)
    fake.missing.add("prod-1")
    fake.recorder.requests.clear()
    return workspace, store


@pytest.mark.parametrize("update", [False, True])
def test_produce_stops_with_a_restart_hint_when_the_stored_production_was_deleted(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic], update: bool
) -> None:
    fake = fake_api([DONE])
    workspace, store = _deleted_in_ui(tmp_path, fake)

    with pytest.raises(typer.BadParameter) as excinfo:
        produce.run_produce(workspace=workspace, dry_run=False, update=update)

    message = str(excinfo.value)
    assert "prod-1 stored in state.json no longer exists in Auphonic" in message
    assert "--update" in message and "`podcast produce --restart`" in message
    # Nothing new was created, updated or started.
    assert fake.created == ["prod-1"]
    assert fake.updates == []
    assert fake.recorder.calls == [("GET", "/api/production/prod-1.json")]
    state = store.read_state()
    assert state.auphonic_production_uuid == "prod-1"
    assert state.auphonic_productions[0].last_known_state == "missing"


def test_production_deleted_while_polling_gets_the_same_hint(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([(4, "Audio Processing"), DONE])
    workspace = _workspace(tmp_path)
    fake.script = [(4, "Audio Processing"), (4, "Audio Processing")]
    original = fake._handle_fixed

    def delete_after_first_poll(request: httpx.Request) -> httpx.Response | None:
        if request.method == "GET" and request.url.path == "/api/production/prod-1.json":
            if fake.polls.get("prod-1", 0) >= 1:
                fake.missing.add("prod-1")
        return original(request)

    fake._handle_fixed = delete_after_first_poll  # type: ignore[method-assign]

    with pytest.raises(typer.BadParameter, match="prod-1 stored in state.json no longer exists"):
        produce.run_produce(workspace=workspace, dry_run=False)
    assert fake.created == ["prod-1"]


def test_restart_after_the_ui_deletion_starts_a_new_production_and_cleanup_records_the_old_one(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([DONE])
    workspace, store = _deleted_in_ui(tmp_path, fake)

    produce.run_produce(workspace=workspace, dry_run=False, restart=True, cleanup=True)

    assert fake.created == ["prod-1", "prod-2"]
    assert _deletes(fake) == []  # 404 on the GET: already gone, nothing to delete
    old, new = store.read_state().auphonic_productions
    assert (old.uuid, old.status, old.last_known_state) == ("prod-1", DELETED, "missing")
    assert old.deleted_at is not None
    assert (new.uuid, new.status) == ("prod-2", CURRENT)


def test_cleanup_refuses_when_the_current_production_was_deleted(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([DONE])
    fake.missing.add("cur")
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    _state_with_history(store, _record("old", SUPERSEDED), _downloaded("cur"))

    with pytest.raises(typer.BadParameter, match="cur stored in state.json no longer exists.*--restart"):
        produce.run_auphonic_cleanup(workspace=workspace, dry_run=False)

    assert _deletes(fake) == []
    records = {record.uuid: record for record in store.read_state().auphonic_productions}
    assert (records["cur"].status, records["cur"].last_known_state) == (CURRENT, "missing")
    assert records["old"].status == SUPERSEDED
    result = CliRunner().invoke(app, ["auphonic-cleanup", "--workspace", str(workspace)])
    assert "cur no longer exists in Auphonic" in result.output


def test_delete_404_is_recorded_as_already_gone(
    tmp_path: Path, fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic]
) -> None:
    fake = fake_api([DONE])
    fake.fixed_status.update({"cur": DONE, "old": (2, "Error")})
    fake.delete_status["old"] = 404
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    _state_with_history(store, _record("old", SUPERSEDED), _downloaded("cur"))

    produce.run_auphonic_cleanup(workspace=workspace, dry_run=False)

    assert _deletes(fake) == ["/api/production/old.json"]
    old = store.read_state().auphonic_productions[0]
    assert (old.status, old.last_known_state) == (DELETED, "error")


# --- automatic cleanup re-checks Auphonic after the download -----------------------


@pytest.mark.parametrize("change", ["changed", "deleted"])
def test_auto_cleanup_rechecks_the_current_production_after_downloading(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic],
    change: str,
) -> None:
    fake = fake_api([DONE])
    workspace = _workspace(tmp_path)
    store = EpisodeWorkspaceStore(workspace)
    produce.run_produce(workspace=workspace, dry_run=False)
    fake.fixed_status["prod-1"] = DONE
    original = fake._handle_fixed

    def change_during_download(request: httpx.Request) -> httpx.Response | None:
        # Someone edits or deletes the new production in the Auphonic web UI while its outputs download.
        if request.url.path.startswith("/download/prod-2"):
            if change == "changed":
                fake.fixed_status["prod-2"] = (15, "Production Changed")
            else:
                fake.missing.add("prod-2")
        return original(request)

    fake._handle_fixed = change_during_download  # type: ignore[method-assign]
    capsys.readouterr()

    produce.run_produce(workspace=workspace, dry_run=False, restart=True, cleanup=True)

    captured = capsys.readouterr()
    assert "Auphonic production complete: prod-2" in captured.out
    assert "Warning: Auphonic cleanup failed: Not deleting superseded Auphonic productions" in captured.err
    assert _deletes(fake) == []
    assert store.read_state().auphonic_productions[0].status == SUPERSEDED


def test_auto_cleanup_local_state_failures_are_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_api: Callable[[list[tuple[int, str]]], _FakeAuphonic],
) -> None:
    fake = fake_api([DONE])
    workspace = _workspace(tmp_path)
    produce.run_produce(workspace=workspace, dry_run=False)
    fake.fixed_status["prod-1"] = DONE

    def failing_mark(*_args: object, **_kwargs: object) -> EpisodeWorkspace:
        raise OSError("state.json is read-only")

    monkeypatch.setattr(produce, "mark_deleted", failing_mark)
    capsys.readouterr()

    produce.run_produce(workspace=workspace, dry_run=False, restart=True, cleanup=True)

    captured = capsys.readouterr()
    assert "Auphonic production complete: prod-2" in captured.out
    assert "Warning: Auphonic cleanup failed: state.json is read-only" in captured.err
    assert fake.deleted == ["prod-1"]

    # The standalone command reports the same failure without a traceback.
    fake.missing.discard("prod-1")
    with pytest.raises(typer.BadParameter, match="state.json is read-only"):
        produce.run_auphonic_cleanup(workspace=workspace, dry_run=False)
