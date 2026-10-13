from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from podcast_pipeline.asset_candidates_llm import generate_single_asset_candidates_llm
from podcast_pipeline.dashboard_context import BackgroundJob, DashboardContext
from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.domain.models import Asset, Candidate, EpisodeWorkspace, ReviewIteration, ReviewVerdict
from podcast_pipeline.entrypoints.dashboard_web import _run_regenerate_job
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(config))


def _workspace(root: Path, chapters: str | None = None) -> EpisodeWorkspaceStore:
    store = EpisodeWorkspaceStore(root)
    store.write_episode_yaml({"episode_id": "test", "inputs": {"chapters": chapters}})
    path = store.layout.episode_summary_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EpisodeSummary(summary_markdown="Summary", key_points=[], topics=[]).model_dump_json())
    return store


@pytest.mark.parametrize("reference", ["absolute", "parent", "symlink", "directory", "missing", "traversal"])
def test_regeneration_rejects_unsafe_chapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("PRIVATE SENTINEL")
    root = tmp_path / "episode"
    root.mkdir()
    (root / "escape.txt").symlink_to(outside)
    (root / "directory").mkdir()
    names = {
        "absolute": str(outside),
        "parent": "../outside.txt",
        "symlink": "escape.txt",
        "directory": "directory",
        "missing": "missing.txt",
        "traversal": "../episode/escape.txt",
    }
    _workspace(root, names[reference])
    runner = Mock()
    monkeypatch.setattr("podcast_pipeline.drafter_runner.DrafterCliRunner", runner)
    original_read = Path.read_text
    reads: list[Path] = []

    def read(path: Path, *args: Any, **kwargs: Any) -> str:
        reads.append(path.resolve())
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(RuntimeError, match="chapters input"):
        generate_single_asset_candidates_llm(workspace=root, asset_id="description")
    assert outside not in reads
    runner.assert_not_called()


def test_regeneration_includes_local_chapters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace(tmp_path, "chapters.txt")
    (tmp_path / "chapters.txt").write_text("00:00 Local intro\n\n00:12 Main topic\n")
    runner = Mock()
    runner.return_value.run.return_value = {"candidates": [{"content": "Generated"}]}
    monkeypatch.setattr("podcast_pipeline.drafter_runner.DrafterCliRunner", runner)
    candidates = generate_single_asset_candidates_llm(
        workspace=tmp_path,
        asset_id="description",
        candidates_per_asset=1,
    )
    prompt = runner.return_value.run.call_args.args[0]
    assert "00:00 Local intro" in prompt
    assert "00:12 Main topic" in prompt
    assert candidates[0].content == "Generated"


@pytest.mark.parametrize("invalid_second", [False, True])
def test_regeneration_preserves_existing_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_second: bool,
) -> None:
    store = _workspace(tmp_path)
    old = Candidate(asset_id="description", content="Selected original")
    candidate_path = store.write_candidate(old)
    review = ReviewIteration(iteration=1, verdict=ReviewVerdict.ok, summary=str(old.candidate_id))
    review_path = store.write_review("description", review)
    store.write_state(
        EpisodeWorkspace(
            episode_id="test",
            root_dir=str(tmp_path),
            assets=[
                Asset(
                    asset_id="description", candidates=[old], selected_candidate_id=old.candidate_id, reviews=[review]
                )
            ],
        )
    )
    original_bytes = {path: path.read_bytes() for path in (candidate_path, review_path, store.layout.state_json)}
    raw = [
        {"candidate_id": str(old.candidate_id), "content": "Alternative A"},
        {"candidate_id": str(old.candidate_id), "content": "Alternative B"},
    ]
    if invalid_second:
        raw[1]["asset_id"] = "wrong"
    runner = Mock()
    runner.return_value.run.return_value = {"candidates": raw}
    monkeypatch.setattr("podcast_pipeline.drafter_runner.DrafterCliRunner", runner)
    ctx = DashboardContext(workspace=tmp_path)
    job = BackgroundJob(job_id="test", stage="regenerate")
    _run_regenerate_job(ctx, job, "description", 2)
    assert job.status == ("failed" if invalid_second else "completed")
    for path, content in original_bytes.items():
        assert path.read_bytes() == content
    paths = list(candidate_path.parent.glob("candidate_*.json"))
    assert len(paths) == (1 if invalid_second else 3)
    if not invalid_second:
        candidates = [Candidate.model_validate_json(path.read_text()) for path in paths]
        assert len({c.candidate_id for c in candidates}) == 3
        assert {c.content for c in candidates} == {old.content, "Alternative A", "Alternative B"}
