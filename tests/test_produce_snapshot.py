import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from podcast_pipeline.auphonic_payload import build_auphonic_payload
from podcast_pipeline.entrypoints import produce
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore
from tests.test_dashboard_guards import _FakeAuphonicClient, _FakeProduction


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAuphonicClient]:
    _FakeAuphonicClient.started = []
    _FakeAuphonicClient.waited = []
    _FakeAuphonicClient.on_start = None
    monkeypatch.setattr(produce, "AuphonicClient", _FakeAuphonicClient)
    monkeypatch.setattr(produce, "load_auphonic_credentials", lambda: object())
    monkeypatch.setattr(produce, "build_auphonic_payload", lambda **kwargs: {})
    return _FakeAuphonicClient


def test_produce_reads_request_after_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_client: type[_FakeAuphonicClient]
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml(
        {
            "episode_id": "synthetic",
            "auphonic": {
                "preset": "synthetic-preset",
                "mode": "single_track",
                "input_file": "https://example.invalid/audio.flac",
                "metadata": {"title": "Before ownership"},
            },
        }
    )
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(tmp_path / "synthetic-global.yaml"))
    monkeypatch.setattr(produce, "build_auphonic_payload", build_auphonic_payload)
    at_lock, release = threading.Event(), threading.Event()
    original_lock = produce._produce_lock

    @contextmanager
    def pause_before_ownership(store: EpisodeWorkspaceStore) -> Iterator[None]:
        at_lock.set()
        assert release.wait(5)
        with original_lock(store):
            yield

    monkeypatch.setattr(produce, "_produce_lock", pause_before_ownership)
    payloads = []
    original_start = fake_client.start_production

    def start(self: _FakeAuphonicClient, payload: dict[str, Any], *, on_created: Any = None) -> _FakeProduction:
        payloads.append(payload)
        return original_start(self, payload, on_created=on_created)

    monkeypatch.setattr(fake_client, "start_production", start)
    failures = []

    def run() -> None:
        try:
            produce.run_produce(workspace=tmp_path, dry_run=False)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert at_lock.wait(5)
        with store.update_episode_yaml() as data:
            data["auphonic"]["metadata"]["title"] = "After ownership"
        release.set()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not failures
    assert len(payloads) == 1
    assert payloads[0]["metadata"]["title"] == "After ownership"


def test_dry_run_never_acquires_production_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_client: type[_FakeAuphonicClient]
) -> None:
    EpisodeWorkspaceStore(tmp_path).write_episode_yaml({"episode_id": "synthetic"})

    def forbidden(store: EpisodeWorkspaceStore) -> None:
        raise AssertionError("dry run acquired production lock")

    monkeypatch.setattr(produce, "_produce_lock", forbidden)
    produce.run_produce(workspace=tmp_path, dry_run=True)
    assert not fake_client.started
