from __future__ import annotations

from pathlib import Path

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
