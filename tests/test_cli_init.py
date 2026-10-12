from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from podcast_pipeline.entrypoints.cli import app
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def test_cli_init_creates_workspace_tree(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"

    result = runner.invoke(
        app,
        [
            "init",
            "--workspace",
            str(workspace),
            "--episode-id",
            "ep_001",
        ],
    )

    assert result.exit_code == 0, result.stdout

    store = EpisodeWorkspaceStore(workspace)
    assert store.read_episode_yaml()["episode_id"] == "ep_001"
    assert store.read_state().episode_id == "ep_001"

    expected_dirs = [
        workspace / "transcript",
        workspace / "transcript" / "chunks",
        workspace / "summaries",
        workspace / "summaries" / "chunks",
        workspace / "summaries" / "episode",
        workspace / "copy" / "candidates",
        workspace / "copy" / "reviews",
        workspace / "copy" / "selected",
        workspace / "copy" / "provenance",
        workspace / "copy" / "protocol",
        workspace / "auphonic",
        workspace / "auphonic" / "downloads",
        workspace / "auphonic" / "outputs",
    ]
    for path in expected_dirs:
        assert path.is_dir()


def test_cli_init_rejects_existing_workspace(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "keep").write_bytes(b"existing workspace")

    result = runner.invoke(
        app,
        [
            "init",
            "--workspace",
            str(workspace),
            "--episode-id",
            "ep_001",
        ],
    )

    assert result.exit_code != 0
    output = result.stdout + result.stderr
    assert "workspace already exists" in output
    assert (workspace / "keep").read_bytes() == b"existing workspace"


def test_cli_init_rejects_invalid_episode_id(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"

    result = runner.invoke(
        app,
        [
            "init",
            "--workspace",
            str(workspace),
            "--episode-id",
            "bad/id",
        ],
    )

    assert result.exit_code != 0
    output = result.stdout + result.stderr
    assert "episode_id" in output


@pytest.mark.parametrize("stage", ["dirs", "yaml", "state"])
def test_init_failure_cleans_owned_root_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    from podcast_pipeline.entrypoints import init

    workspace = tmp_path / "workspace"
    args = ["init", "--workspace", str(workspace), "--episode-id", "ep_001"]
    called = False

    def fail(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True
        raise OSError("injected failure")

    with monkeypatch.context() as patch:
        if stage == "dirs":
            patch.setattr(init, "_create_workspace_dirs", fail)
        else:
            patch.setattr(EpisodeWorkspaceStore, "write_episode_yaml" if stage == "yaml" else "write_state", fail)
        result = CliRunner().invoke(app, args)
    assert called
    assert result.exit_code != 0
    assert "failed to initialize workspace" in result.stdout + result.stderr
    assert "injected failure" in result.stdout + result.stderr
    assert not workspace.exists()
    assert CliRunner().invoke(app, args).exit_code == 0


def test_init_failure_preserves_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from podcast_pipeline.entrypoints import init

    workspace = tmp_path / "workspace"
    moved = tmp_path / "moved"

    def replace(layout: object) -> None:
        workspace.rename(moved)
        workspace.mkdir()
        (workspace / "keep").write_bytes(b"untouched")
        raise OSError("injected replacement")

    monkeypatch.setattr(init, "_create_workspace_dirs", replace)
    result = CliRunner().invoke(app, ["init", "--workspace", str(workspace), "--episode-id", "ep_001"])
    assert result.exit_code != 0
    assert (workspace / "keep").read_bytes() == b"untouched"
    assert "replacement workspace preserved" in result.stdout + result.stderr


def test_init_reports_original_and_cleanup_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from podcast_pipeline.entrypoints import init

    workspace = tmp_path / "workspace"

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("state write failed")

    def cleanup_fail(*args: object, **kwargs: object) -> None:
        raise OSError("cleanup denied")

    monkeypatch.setattr(EpisodeWorkspaceStore, "write_state", fail)
    monkeypatch.setattr(init.shutil, "rmtree", cleanup_fail)
    result = CliRunner().invoke(app, ["init", "--workspace", str(workspace), "--episode-id", "ep_001"])
    assert result.exit_code != 0
    output = result.stdout + result.stderr
    assert "state write failed" in output
    assert "cleanup failed: cleanup denied" in output
    assert workspace.is_dir()
