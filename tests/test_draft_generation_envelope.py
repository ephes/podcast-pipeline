from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from podcast_pipeline import asset_candidates_stub as stub
from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.entrypoints.cli import app
from podcast_pipeline.entrypoints.draft_candidates import run_draft_candidates
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


@pytest.mark.parametrize(
    "raw",
    [
        '{"summary_markdown":"first","summary_markdown":"second"}',
        '{"summary_markdown":"synthetic","key_points":[],"key_points":["other"]}',
        '{"summary_markdown":"synthetic","topics":[NaN]}',
    ],
)
def test_summary_evidence_is_strict_before_writes(tmp_path: Path, raw: str) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "synthetic"})
    path = store.layout.episode_summary_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw)
    with pytest.raises(typer.BadParameter, match="summary"):
        run_draft_candidates(workspace=tmp_path, chapters=None, candidates_per_asset=1)
    assert not (tmp_path / "copy").exists()


@pytest.mark.parametrize("count", [11, True, False, 0, -1, 1.5, "2", None])
def test_invalid_count_never_renders(count: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(**kwargs: Any) -> str:
        pytest.fail("invalid count must not render")

    monkeypatch.setattr(stub, "_render_asset_content", forbidden)
    with pytest.raises(ValueError, match="candidates_per_asset"):
        stub.generate_draft_candidates(
            episode_summary=EpisodeSummary(summary_markdown="synthetic"),
            chapters=[],
            config=stub.DraftCandidatesConfig(candidates_per_asset=count),
        )


def test_invalid_config_is_controlled() -> None:
    invalid: Any = {"candidates_per_asset": 1}
    with pytest.raises(ValueError, match="config"):
        stub.generate_draft_candidates(
            episode_summary=EpisodeSummary(summary_markdown="synthetic"), chapters=[], config=invalid
        )


def test_at_limit_is_exact_and_deterministic() -> None:
    summary = EpisodeSummary(summary_markdown="synthetic")
    config = stub.DraftCandidatesConfig(candidates_per_asset=10)
    result = stub.generate_draft_candidates(episode_summary=summary, chapters=[], config=config)
    assert all(len(candidates) == 10 for candidates in result.values())
    assert all(len({c.candidate_id for c in candidates}) == 10 for candidates in result.values())
    assert result == stub.generate_draft_candidates(episode_summary=summary, chapters=[], config=config)


def test_cli_rejects_excess_count_before_inputs(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["draft-candidates", "--workspace", str(tmp_path), "-n", "11"])
    assert result.exit_code == 2
    assert "10" in result.output and "candidates" in result.output
    assert not list(tmp_path.iterdir())
