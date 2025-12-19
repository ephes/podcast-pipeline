from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from podcast_pipeline.entrypoints.cli import app

from .golden_utils import assert_workspace_matches_golden


def test_cli_summarize_dry_run_writes_artifacts(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "pp_068"
    transcript = fixture_dir / "transcript.txt"

    result = runner.invoke(
        app,
        [
            "summarize",
            "--dry-run",
            "--workspace",
            str(workspace),
            "--episode-id",
            "ep_068",
            "--transcript",
            str(transcript),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert (workspace / "episode.yaml").exists()
    assert (workspace / "state.json").exists()
    assert (workspace / "transcript" / "transcript.txt").exists()

    chunk_summary = workspace / "summaries" / "chunks" / "chunk_0001.summary.json"
    assert chunk_summary.exists()
    loaded = json.loads(chunk_summary.read_text(encoding="utf-8"))
    assert loaded["chunk_id"] == 1
    assert loaded["summary_markdown"].strip()

    assert (workspace / "summaries" / "episode" / "episode_summary.json").exists()
    assert (workspace / "summaries" / "episode" / "episode_summary.md").exists()
    assert (workspace / "summaries" / "episode" / "episode_summary.html").exists()

    golden = Path(__file__).resolve().parent / "golden" / "summarize_pp_068_dry_run"
    assert_workspace_matches_golden(workspace=workspace, golden=golden)
