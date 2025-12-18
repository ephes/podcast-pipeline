from __future__ import annotations

from pathlib import Path

import pytest

from podcast_pipeline.agent_runners import FakeCreatorRunner, FakeReviewerRunner
from podcast_pipeline.domain.models import Candidate, ReviewVerdict
from podcast_pipeline.review_loop_engine import CreatorInput, ReviewerInput
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


def test_fake_creator_runner_is_deterministic_by_default(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    replies = [{"done": False, "candidate": {"content": "draft 1"}}]

    runner_a = FakeCreatorRunner(layout=layout, replies=replies)
    runner_b = FakeCreatorRunner(layout=layout, replies=replies)

    inp = CreatorInput(asset_id="description", iteration=1, previous_candidate=None, previous_review=None)
    out_a = runner_a(inp)
    out_b = runner_b(inp)

    assert out_a.done is False
    assert out_a.candidate.model_dump(mode="json") == out_b.candidate.model_dump(mode="json")


def test_fake_creator_runner_can_mutate_files(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)

    runner = FakeCreatorRunner(
        layout=layout,
        replies=[
            {
                "done": True,
                "candidate": {"content": "final"},
                "mutate_files": {"copy/selected/description.md": "# Title\n"},
            }
        ],
    )

    runner(CreatorInput(asset_id="description", iteration=1, previous_candidate=None, previous_review=None))
    assert (tmp_path / "copy" / "selected" / "description.md").read_text(encoding="utf-8") == "# Title\n"


def test_fake_reviewer_runner_defaults_are_deterministic(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    replies = [{"verdict": "changes_requested", "issues": [{"message": "fix this"}]}]

    runner_a = FakeReviewerRunner(layout=layout, replies=replies, reviewer="reviewer_a")
    runner_b = FakeReviewerRunner(layout=layout, replies=replies, reviewer="reviewer_a")

    candidate = Candidate(asset_id="description", content="draft")
    inp = ReviewerInput(asset_id="description", iteration=2, candidate=candidate)
    out_a = runner_a(inp)
    out_b = runner_b(inp)

    assert out_a.verdict == ReviewVerdict.changes_requested
    assert out_a.model_dump(mode="json") == out_b.model_dump(mode="json")


def test_fake_runners_raise_when_script_exhausted(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)

    creator = FakeCreatorRunner(layout=layout, replies=[{"done": True, "candidate": {"content": "x"}}])
    creator(CreatorInput(asset_id="description", iteration=1, previous_candidate=None, previous_review=None))
    with pytest.raises(IndexError):
        creator(CreatorInput(asset_id="description", iteration=2, previous_candidate=None, previous_review=None))
