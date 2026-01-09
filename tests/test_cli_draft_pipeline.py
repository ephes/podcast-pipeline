from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from podcast_pipeline.domain.models import AssetKind
from podcast_pipeline.entrypoints.cli import app


def _fixture_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "pp_068"


def test_cli_draft_dry_run_writes_pipeline_artifacts(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"

    result = runner.invoke(
        app,
        [
            "draft",
            "--dry-run",
            "--workspace",
            str(workspace),
            "--episode-id",
            "ep_068",
            "--transcript",
            str(_fixture_dir() / "transcript.txt"),
            "--chapters",
            str(_fixture_dir() / "chapters.txt"),
            "--candidates",
            "2",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert (workspace / "episode.yaml").exists()
    assert (workspace / "state.json").exists()
    assert (workspace / "transcript" / "transcript.txt").exists()
    assert (workspace / "transcript" / "chapters.txt").exists()
    assert (workspace / "transcript" / "chunks" / "chunk_0001.txt").exists()
    assert (workspace / "summaries" / "chunks" / "chunk_0001.summary.json").exists()
    assert (workspace / "summaries" / "episode" / "episode_summary.json").exists()
    assert (workspace / "summaries" / "episode" / "episode_summary.md").exists()
    assert (workspace / "summaries" / "episode" / "episode_summary.html").exists()

    for kind in AssetKind:
        asset_dir = workspace / "copy" / "candidates" / kind.value
        assert asset_dir.exists()
        assert len(list(asset_dir.glob("candidate_*.json"))) == 2
        assert len(list(asset_dir.glob("candidate_*.md"))) == 2
        assert len(list(asset_dir.glob("candidate_*.html"))) == 2
