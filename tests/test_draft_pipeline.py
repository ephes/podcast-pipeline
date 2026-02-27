from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from podcast_pipeline.domain.models import AssetKind
from podcast_pipeline.entrypoints.draft_pipeline import (
    _clear_stale_artifacts,
    _discover_chunk_ids,
    _ingest_transcript,
    run_draft_pipeline,
)
from podcast_pipeline.summarization_stub import StubSummarizerConfig
from podcast_pipeline.transcript_chunker import ChunkerConfig
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def _make_workspace(tmp_path: Path) -> EpisodeWorkspaceStore:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "test"})
    return store


def test_discover_chunk_ids_empty(tmp_path: Path) -> None:
    store = _make_workspace(tmp_path)
    assert _discover_chunk_ids(store) == []


def test_discover_chunk_ids_finds_existing(tmp_path: Path) -> None:
    store = _make_workspace(tmp_path)
    chunks_dir = store.layout.transcript_chunks_dir
    chunks_dir.mkdir(parents=True)
    (chunks_dir / "chunk_0001.txt").write_text("hello\n")
    (chunks_dir / "chunk_0002.txt").write_text("world\n")
    assert _discover_chunk_ids(store) == [1, 2]


def test_clear_stale_artifacts_removes_chunks_and_summaries(tmp_path: Path) -> None:
    store = _make_workspace(tmp_path)

    # Create chunks
    chunks_dir = store.layout.transcript_chunks_dir
    chunks_dir.mkdir(parents=True)
    (chunks_dir / "chunk_0001.txt").write_text("hello\n")

    # Create summaries
    summaries_dir = store.layout.summaries_dir
    chunk_summaries_dir = store.layout.chunk_summaries_dir
    chunk_summaries_dir.mkdir(parents=True)
    (chunk_summaries_dir / "chunk_0001.summary.json").write_text("{}\n")

    episode_dir = store.layout.episode_summary_dir
    episode_dir.mkdir(parents=True)
    (episode_dir / "episode_summary.json").write_text("{}\n")

    _clear_stale_artifacts(store)

    assert not chunks_dir.exists()
    assert not summaries_dir.exists()


def test_ingest_transcript_clears_stale_and_copies(tmp_path: Path) -> None:
    store = _make_workspace(tmp_path)

    # Create existing stale chunks
    chunks_dir = store.layout.transcript_chunks_dir
    chunks_dir.mkdir(parents=True)
    (chunks_dir / "chunk_0001.txt").write_text("old chunk\n")

    # Create a source transcript
    source = tmp_path / "source_transcript.txt"
    source.write_text("new transcript content\n")

    _ingest_transcript(store=store, transcript=source)

    # Old chunks should be gone
    assert not chunks_dir.exists()

    # New transcript should be in place
    transcript_dest = store.layout.transcript_dir / "transcript.txt"
    assert transcript_dest.exists()
    assert transcript_dest.read_text() == "new transcript content\n"

    # episode.yaml should reference the new transcript
    episode_yaml = store.read_episode_yaml()
    inputs = episode_yaml.get("inputs", {})
    assert "transcript" in inputs


# ---------------------------------------------------------------------------
# End-to-end tests for run_draft_pipeline (non-dry-run) with fake runner
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Fake DrafterCliRunner that returns canned responses based on prompt content."""

    def __init__(self) -> None:
        self.call_count = 0
        self.prompts: list[str] = []

    def run(self, prompt_text: str) -> dict[str, Any]:
        self.call_count += 1
        self.prompts.append(prompt_text)
        # Chunk summary prompt
        if "Transcript chunk:" in prompt_text:
            return {
                "summary_markdown": "## Zusammenfassung\n\nStub\n",
                "bullets": ["punkt"],
                "entities": ["Entity"],
            }
        # Episode summary prompt
        if "Chunk summaries" in prompt_text:
            return {
                "summary_markdown": "# Episode\n\nStub episode\n",
                "key_points": ["key"],
                "topics": ["topic"],
            }
        # Asset candidates prompt
        if "Asset type:" in prompt_text:
            # Extract asset_id from prompt
            for line in prompt_text.splitlines():
                if line.startswith("Asset type:"):
                    asset_id = line.split(":", 1)[1].strip()
                    break
            else:
                asset_id = "unknown"
            return {
                "candidates": [
                    {"asset_id": asset_id, "content": f"fake {asset_id}"},
                ],
            }
        return {}


def _write_transcript_file(tmp_path: Path) -> Path:
    transcript = tmp_path / "transcript.txt"
    transcript.write_text(
        "Speaker 1: Hallo und willkommen.\n" * 20,
        encoding="utf-8",
    )
    return transcript


def test_run_draft_pipeline_non_dry_run_new_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full non-dry-run pipeline: new workspace, transcript → chunks → summary → candidates."""
    workspace = tmp_path / "ws"
    transcript = _write_transcript_file(tmp_path)
    fake_runner = _FakeRunner()

    # Patch DrafterCliRunner on the source module; _run_llm_pipeline
    # imports it locally so patching the origin is sufficient.
    import podcast_pipeline.drafter_runner as dr_mod

    class _PatchedCliRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run(self, prompt_text: str) -> dict[str, Any]:
            return fake_runner.run(prompt_text)

    monkeypatch.setattr(dr_mod, "DrafterCliRunner", _PatchedCliRunner)

    run_draft_pipeline(
        dry_run=False,
        workspace=workspace,
        episode_id="test_ep",
        transcript=transcript,
        chapters=None,
        candidates_per_asset=1,
        chunker_config=ChunkerConfig(),
        summarizer_config=StubSummarizerConfig(),
        timeout_seconds=None,
    )

    store = EpisodeWorkspaceStore(workspace)

    # Episode summary should exist
    assert store.layout.episode_summary_json_path().exists()
    summary = json.loads(store.layout.episode_summary_json_path().read_text())
    assert "summary_markdown" in summary

    # Candidates should exist for each asset type
    for kind in AssetKind:
        candidates_dir = store.layout.copy_candidates_dir / kind.value
        assert candidates_dir.exists(), f"Missing candidates for {kind.value}"
        json_files = list(candidates_dir.glob("candidate_*.json"))
        assert len(json_files) >= 1

    # Runner was called: chunks + episode summary + 13 asset types
    assert fake_runner.call_count > 0


def test_run_draft_pipeline_reuses_existing_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When episode summary already exists, summarization is skipped."""
    workspace = tmp_path / "ws"
    workspace.mkdir()

    store = EpisodeWorkspaceStore(workspace)
    store.write_episode_yaml({"episode_id": "test_ep"})

    # Pre-populate transcript and chunks
    transcript_dir = store.layout.transcript_dir
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "transcript.txt").write_text("Hallo welt.\n")
    chunks_dir = store.layout.transcript_chunks_dir
    chunks_dir.mkdir(parents=True)
    (chunks_dir / "chunk_0001.txt").write_text("Hallo welt.\n")

    # Pre-populate episode summary
    summary_dir = store.layout.episode_summary_dir
    summary_dir.mkdir(parents=True)
    summary_data = {
        "version": 1,
        "summary_markdown": "# Existing\n\nAlready done\n",
        "key_points": ["existing"],
        "topics": ["existing_topic"],
    }
    store.layout.episode_summary_json_path().write_text(
        json.dumps(summary_data, indent=2) + "\n",
    )

    fake_runner = _FakeRunner()

    import podcast_pipeline.drafter_runner as dr_mod

    class _PatchedCliRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run(self, prompt_text: str) -> dict[str, Any]:
            return fake_runner.run(prompt_text)

    monkeypatch.setattr(dr_mod, "DrafterCliRunner", _PatchedCliRunner)

    run_draft_pipeline(
        dry_run=False,
        workspace=workspace,
        episode_id="test_ep",
        transcript=None,
        chapters=None,
        candidates_per_asset=1,
        chunker_config=ChunkerConfig(),
        summarizer_config=StubSummarizerConfig(),
        timeout_seconds=None,
    )

    # Summary should still contain the pre-existing content (not overwritten)
    summary = json.loads(store.layout.episode_summary_json_path().read_text())
    assert summary["summary_markdown"] == "# Existing\n\nAlready done\n"

    # Runner should only have been called for asset candidates (13), not for summarization
    assert fake_runner.call_count == len(AssetKind)


def test_run_draft_pipeline_transcript_override_invalidates_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Providing --transcript on an existing workspace clears stale chunks + summaries."""
    workspace = tmp_path / "ws"
    workspace.mkdir()

    store = EpisodeWorkspaceStore(workspace)
    store.write_episode_yaml({"episode_id": "test_ep"})

    # Pre-populate old chunks and summary
    chunks_dir = store.layout.transcript_chunks_dir
    chunks_dir.mkdir(parents=True)
    (chunks_dir / "chunk_0001.txt").write_text("old content\n")
    summary_dir = store.layout.episode_summary_dir
    summary_dir.mkdir(parents=True)
    store.layout.episode_summary_json_path().write_text('{"old": true}\n')

    new_transcript = tmp_path / "new_transcript.txt"
    new_transcript.write_text("Speaker: Neuer Inhalt hier.\n" * 20)

    fake_runner = _FakeRunner()

    import podcast_pipeline.drafter_runner as dr_mod

    class _PatchedCliRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run(self, prompt_text: str) -> dict[str, Any]:
            return fake_runner.run(prompt_text)

    monkeypatch.setattr(dr_mod, "DrafterCliRunner", _PatchedCliRunner)

    run_draft_pipeline(
        dry_run=False,
        workspace=workspace,
        episode_id="test_ep",
        transcript=new_transcript,
        chapters=None,
        candidates_per_asset=1,
        chunker_config=ChunkerConfig(),
        summarizer_config=StubSummarizerConfig(),
        timeout_seconds=None,
    )

    # Summary should now be the LLM-generated one, not the old one
    summary = json.loads(store.layout.episode_summary_json_path().read_text())
    assert "old" not in summary
    assert "summary_markdown" in summary

    # Runner was called for chunks + episode summary + assets
    assert fake_runner.call_count > len(AssetKind)


def test_run_draft_pipeline_hosts_persisted_and_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosts from --host are persisted to episode.yaml and reused on subsequent runs."""
    workspace = tmp_path / "ws"
    transcript = _write_transcript_file(tmp_path)
    fake_runner = _FakeRunner()

    import podcast_pipeline.drafter_runner as dr_mod

    class _PatchedCliRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run(self, prompt_text: str) -> dict[str, Any]:
            return fake_runner.run(prompt_text)

    monkeypatch.setattr(dr_mod, "DrafterCliRunner", _PatchedCliRunner)

    # First run with --host flags
    run_draft_pipeline(
        dry_run=False,
        workspace=workspace,
        episode_id="test_ep",
        transcript=transcript,
        chapters=None,
        candidates_per_asset=1,
        chunker_config=ChunkerConfig(),
        summarizer_config=StubSummarizerConfig(),
        timeout_seconds=None,
        hosts=["Jochen", "Dominik"],
    )

    # Verify hosts persisted in episode.yaml
    store = EpisodeWorkspaceStore(workspace)
    episode_yaml = store.read_episode_yaml()
    assert episode_yaml["hosts"] == ["Jochen", "Dominik"]

    # Verify hosts appeared in summarization prompts
    chunk_prompts = [p for p in fake_runner.prompts if "Transcript chunk:" in p]
    assert len(chunk_prompts) > 0
    for prompt in chunk_prompts:
        assert "Jochen, Dominik" in prompt

    episode_prompts = [p for p in fake_runner.prompts if "Chunk summaries" in p]
    assert len(episode_prompts) > 0
    for prompt in episode_prompts:
        assert "Jochen, Dominik" in prompt

    # Verify hosts appeared in candidate prompts
    asset_prompts = [p for p in fake_runner.prompts if "Asset type:" in p]
    assert len(asset_prompts) > 0
    for prompt in asset_prompts:
        assert "Jochen, Dominik" in prompt

    # Second run WITHOUT --host flags (should fall back to episode.yaml)
    fake_runner2 = _FakeRunner()

    class _PatchedCliRunner2:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run(self, prompt_text: str) -> dict[str, Any]:
            return fake_runner2.run(prompt_text)

    monkeypatch.setattr(dr_mod, "DrafterCliRunner", _PatchedCliRunner2)

    run_draft_pipeline(
        dry_run=False,
        workspace=workspace,
        episode_id="test_ep",
        transcript=None,
        chapters=None,
        candidates_per_asset=1,
        chunker_config=ChunkerConfig(),
        summarizer_config=StubSummarizerConfig(),
        timeout_seconds=None,
        hosts=None,  # not provided this time
    )

    # Hosts should still appear in prompts from episode.yaml fallback
    asset_prompts2 = [p for p in fake_runner2.prompts if "Asset type:" in p]
    assert len(asset_prompts2) > 0
    for prompt in asset_prompts2:
        assert "Jochen, Dominik" in prompt


def test_run_draft_pipeline_dry_run_persists_hosts(
    tmp_path: Path,
) -> None:
    """In dry-run mode, --host flags are still persisted to episode.yaml."""
    workspace = tmp_path / "ws"
    transcript = _write_transcript_file(tmp_path)

    run_draft_pipeline(
        dry_run=True,
        workspace=workspace,
        episode_id="test_ep",
        transcript=transcript,
        chapters=None,
        candidates_per_asset=1,
        chunker_config=ChunkerConfig(),
        summarizer_config=StubSummarizerConfig(),
        timeout_seconds=None,
        hosts=["Jochen", "Dominik"],
    )

    store = EpisodeWorkspaceStore(workspace)
    episode_yaml = store.read_episode_yaml()
    assert episode_yaml["hosts"] == ["Jochen", "Dominik"]
