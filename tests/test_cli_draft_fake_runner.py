from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner, Result

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


def _review(workspace: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        app, ["review", "--fake-runner", "--workspace", str(workspace), "--episode-id", "ep_001", *extra]
    )


def test_cli_review_terminal_resume_reports_no_agent_calls(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert _review(workspace).exit_code == 0
    again = _review(workspace)
    assert again.exit_code == 0, again.output
    assert "no agent calls made" in again.output
    assert "Selected:" not in again.output


def test_cli_review_protocol_error_exits_cleanly(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert _review(workspace).exit_code == 0
    protocol = workspace / "copy" / "protocol" / "description" / "state.json"
    protocol.write_text("{not json", encoding="utf-8")
    result = _review(workspace)
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Review stopped" in result.output


def test_cli_review_reports_immutable_companion_conflict(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert _review(workspace).exit_code == 0
    # Simulate interruption after candidate publication and before the resume marker.
    (workspace / "copy" / "protocol" / "description" / "state.json").unlink()
    candidate_dir = workspace / "copy" / "candidates" / "description"
    html = next(candidate_dir.glob("candidate_*.html"))
    conflict = b"Conflicting synthetic HTML"
    html.write_bytes(conflict)

    result = _review(workspace)

    assert result.exit_code == 1
    assert f"Review stopped: Immutable candidate companion conflicts at {html}" in result.stderr
    assert html.read_bytes() == conflict


def test_cli_review_corrupt_candidate_evidence_exits_cleanly(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert _review(workspace).exit_code == 0
    candidate = next((workspace / "copy" / "candidates" / "description").glob("candidate_*.json"))
    candidate.write_text('{"asset_id": "description", "content": "private draft text"', encoding="utf-8")
    result = _review(workspace)
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Review stopped" in result.output
    assert "copy/candidates/description/" in result.output
    # Content-free: no decoder detail or saved candidate text in the diagnostic.
    assert "private draft text" not in result.output
    assert "Expecting" not in result.output
