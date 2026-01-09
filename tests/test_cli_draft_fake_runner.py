from __future__ import annotations

from pathlib import Path

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
