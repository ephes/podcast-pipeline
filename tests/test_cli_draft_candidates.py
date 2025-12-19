from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from podcast_pipeline.domain.models import AssetKind
from podcast_pipeline.entrypoints.cli import app


def _fixture_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "pp_068"


def test_cli_draft_candidates_writes_n_candidates_per_asset(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"

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
            str(_fixture_dir() / "transcript.txt"),
        ],
    )
    assert result.exit_code == 0, result.stdout

    chapters = _fixture_dir() / "chapters.txt"

    result = runner.invoke(
        app,
        [
            "draft-candidates",
            "--workspace",
            str(workspace),
            "--chapters",
            str(chapters),
            "--candidates",
            "2",
        ],
    )
    assert result.exit_code == 0, result.stdout

    before: dict[str, set[str]] = {}
    for kind in AssetKind:
        asset_dir = workspace / "copy" / "candidates" / kind.value
        assert asset_dir.exists()
        json_files = sorted(p.name for p in asset_dir.glob("candidate_*.json"))
        md_files = sorted(p.name for p in asset_dir.glob("candidate_*.md"))
        html_files = sorted(p.name for p in asset_dir.glob("candidate_*.html"))
        assert len(json_files) == 2
        assert len(md_files) == 2
        assert len(html_files) == 2
        before[kind.value] = set(json_files + md_files + html_files)

    result = runner.invoke(
        app,
        [
            "draft-candidates",
            "--workspace",
            str(workspace),
            "--chapters",
            str(chapters),
            "--candidates",
            "2",
        ],
    )
    assert result.exit_code == 0, result.stdout

    for kind in AssetKind:
        asset_dir = workspace / "copy" / "candidates" / kind.value
        after = set(p.name for p in asset_dir.glob("candidate_*.*"))
        assert after == before[kind.value]
