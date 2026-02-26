from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from podcast_pipeline.entrypoints.cli import app


def test_cli_review_fake_runner_creates_workspace(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"

    result = runner.invoke(
        app,
        [
            "review",
            "--fake-runner",
            "--workspace",
            str(workspace),
            "--episode-id",
            "ep_001",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert (workspace / "episode.yaml").exists()
    assert (workspace / "state.json").exists()
    assert (workspace / "copy" / "protocol" / "description" / "state.json").exists()
    assert (workspace / "copy" / "selected" / "description.md").exists()

    candidates_dir = workspace / "copy" / "candidates" / "description"
    assert candidates_dir.exists()
    assert list(candidates_dir.glob("candidate_*.json"))


def test_cli_review_fake_runner_uses_existing_workspace(tmp_path: Path) -> None:
    """Create a workspace manually, then run review --fake-runner against it."""
    workspace = tmp_path / "existing_ws"
    workspace.mkdir()

    # Set up a minimal workspace with episode.yaml, state.json, and transcript
    transcript_dir = workspace / "transcript"
    transcript_dir.mkdir()
    transcript_path = transcript_dir / "transcript.txt"
    transcript_path.write_text("Speaker 1: Testing existing workspace.\n", encoding="utf-8")
    chapters_path = transcript_dir / "chapters.txt"
    chapters_path.write_text("00:00 Test Intro\n", encoding="utf-8")

    episode_yaml = {
        "episode_id": "existing_ep",
        "inputs": {
            "transcript": "transcript/transcript.txt",
            "chapters": "transcript/chapters.txt",
        },
    }
    (workspace / "episode.yaml").write_text(yaml.safe_dump(episode_yaml), encoding="utf-8")
    state = {"episode_id": "existing_ep", "root_dir": str(workspace)}
    (workspace / "state.json").write_text(json.dumps(state), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "review",
            "--fake-runner",
            "--workspace",
            str(workspace),
            "--episode-id",
            "existing_ep",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert (workspace / "copy" / "protocol" / "description" / "state.json").exists()
    assert (workspace / "copy" / "selected" / "description.md").exists()


def test_cli_review_existing_workspace_without_episode_yaml_fails(tmp_path: Path) -> None:
    """An existing workspace without episode.yaml should fail."""
    workspace = tmp_path / "bad_ws"
    workspace.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "review",
            "--fake-runner",
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code != 0
    output = (result.stdout or "") + (result.output if hasattr(result, "output") else "")
    assert "episode.yaml" in output
