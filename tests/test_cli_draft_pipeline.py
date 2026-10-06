from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from podcast_pipeline.agent_runners import AgentRunnerError
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


def test_cli_draft_reports_agent_cli_timeout_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents:\n  drafter:\n    command: sleep\n    args: ['30']\n", encoding="utf-8")
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(config_path))
    monkeypatch.delenv("PODCAST_PIPELINE_AGENT_TIMEOUT", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "draft",
            "--workspace",
            str(tmp_path / "workspace"),
            "--transcript",
            str(_fixture_dir() / "transcript.txt"),
            "--timeout",
            "0.3",
        ],
    )

    assert result.exit_code == 1
    assert "Drafter CLI timed out after 0.3 s" in result.output
    assert not isinstance(result.exception, AgentRunnerError)
