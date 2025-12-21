from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from podcast_pipeline.entrypoints.cli import app


def test_cli_status_reports_latest_review(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"

    result = runner.invoke(
        app,
        [
            "draft",
            "--fake-runner",
            "--workspace",
            str(workspace),
        ],
    )
    assert result.exit_code == 0, result.stdout

    result = runner.invoke(
        app,
        [
            "status",
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "Asset: description" in result.stdout
    assert "Iteration: 2/3" in result.stdout
    assert "Verdict: ok" in result.stdout
    assert "Outcome: converged" in result.stdout
    assert "Blocking issues: none" in result.stdout
