from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from podcast_pipeline.domain.models import (
    Candidate,
    EpisodeWorkspace,
    ProvenanceRef,
    ReviewIteration,
    ReviewVerdict,
    TextFormat,
)
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore


def test_layout_paths(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    assert layout.episode_yaml == tmp_path / "episode.yaml"
    assert layout.state_json == tmp_path / "state.json"
    assert layout.copy_candidates_dir == tmp_path / "copy" / "candidates"
    assert layout.copy_reviews_dir == tmp_path / "copy" / "reviews"
    assert layout.copy_selected_dir == tmp_path / "copy" / "selected"
    assert layout.copy_provenance_dir == tmp_path / "copy" / "provenance"


def test_layout_copy_paths_are_deterministic(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)

    candidate_id = UUID("01234567-89ab-cdef-0123-456789abcdef")
    assert layout.candidate_json_path("description", candidate_id) == (
        tmp_path / "copy" / "candidates" / "description" / f"candidate_{candidate_id}.json"
    )

    assert layout.review_iteration_json_path("description", 3, reviewer="reviewer_a") == (
        tmp_path / "copy" / "reviews" / "description" / "iteration_03.reviewer_a.json"
    )

    assert layout.selected_text_path("description", TextFormat.markdown) == (
        tmp_path / "copy" / "selected" / "description.md"
    )

    assert layout.provenance_json_path("codex", "run_001") == (
        tmp_path / "copy" / "provenance" / "codex" / "run_001.json"
    )


def test_store_reads_writes_episode_yaml(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep_001", "sources": {"reaper_media_dir": "/tmp"}})
    loaded = store.read_episode_yaml()
    assert loaded["episode_id"] == "ep_001"
    assert loaded["sources"]["reaper_media_dir"] == "/tmp"


def test_store_reads_writes_state_json(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    created_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    workspace = EpisodeWorkspace(episode_id="ep_001", root_dir=str(tmp_path), created_at=created_at)
    store.write_state(workspace)
    loaded = store.read_state()
    assert loaded.model_dump(mode="json") == workspace.model_dump(mode="json")


def test_store_reads_writes_copy_artifacts(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    created_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)

    candidate_id = UUID("01234567-89ab-cdef-0123-456789abcdef")
    candidate = Candidate(
        candidate_id=candidate_id,
        asset_id="description",
        format=TextFormat.markdown,
        content="# Hello\n",
        created_at=created_at,
    )
    candidate_path = store.write_candidate(candidate)
    assert candidate_path.exists()
    assert store.read_candidate("description", candidate_id).model_dump(mode="json") == candidate.model_dump(
        mode="json"
    )

    review = ReviewIteration(iteration=1, verdict=ReviewVerdict.ok, reviewer="reviewer_a", created_at=created_at)
    review_path = store.write_review("description", review)
    assert review_path.exists()
    assert store.read_review("description", 1, reviewer="reviewer_a").model_dump(mode="json") == review.model_dump(
        mode="json"
    )

    selected_path = store.write_selected_text("description", TextFormat.markdown, "final\n")
    assert selected_path.exists()
    assert store.read_selected_text("description", TextFormat.markdown) == "final\n"

    provenance = ProvenanceRef(kind="codex", ref="run_001", created_at=created_at)
    provenance_path = store.write_provenance_json(provenance, {"ok": True})
    assert provenance_path.exists()


def test_layout_rejects_path_separators(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    with pytest.raises(ValueError):
        layout.candidate_json_path("a/b", UUID("01234567-89ab-cdef-0123-456789abcdef"))
