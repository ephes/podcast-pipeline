from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.domain.models import AssetKind, EpisodeWorkspace
from podcast_pipeline.entrypoints.cli import app
from podcast_pipeline.pick_core import candidate_generation, load_candidates, save_candidate_selection
from podcast_pipeline.summarization_stub import write_episode_summary_artifacts
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


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


def test_changed_standalone_draft_preserves_selected_candidate(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "synthetic"})
    chapters = tmp_path / "chapters.txt"
    chapters.write_text("00:00 Synthetic introduction\n", encoding="utf-8")
    write_episode_summary_artifacts(
        layout=store.layout, episode_summary=EpisodeSummary(summary_markdown="Synthetic episode")
    )
    runner = CliRunner()
    args = ["draft-candidates", "--workspace", str(tmp_path), "--chapters", str(chapters), "-n", "1"]

    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    [original] = load_candidates(layout=store.layout, asset_id="description")["description"]
    generation = candidate_generation(layout=store.layout, candidate=original)
    save_candidate_selection(
        store=store,
        workspace=EpisodeWorkspace(episode_id="synthetic", root_dir=str(tmp_path)),
        candidate=original,
        candidates=[original],
        selected_format=original.format,
        selected_text=original.content,
    )
    selected_path = store.layout.selected_text_path(original.asset_id, original.format)
    selected_before = selected_path.read_bytes()

    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert load_candidates(layout=store.layout, asset_id="description")["description"] == [original]

    chapters.write_text("00:00 Synthetic closing remarks\n", encoding="utf-8")
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    candidates = load_candidates(layout=store.layout, asset_id="description")["description"]
    assert len(candidates) == 2
    [new] = [candidate for candidate in candidates if candidate.candidate_id != original.candidate_id]
    assert "Synthetic closing remarks" in new.content
    assert store.read_candidate(original.asset_id, original.candidate_id) == original
    assert candidate_generation(layout=store.layout, candidate=original) == generation
    assert store.read_state().assets[0].selected_candidate_id == original.candidate_id
    assert selected_path.read_bytes() == selected_before
