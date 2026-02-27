from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from podcast_pipeline.domain import (
    Asset,
    Candidate,
    Chapter,
    EpisodeWorkspace,
    ReviewIssue,
    ReviewIteration,
    ReviewVerdict,
)


def test_workspace_roundtrip_json() -> None:
    asset = Asset(
        asset_id="description",
        candidates=[
            Candidate(asset_id="description", content="# Hello\n\nWorld"),
        ],
        reviews=[ReviewIteration(iteration=1, verdict=ReviewVerdict.ok)],
    )
    ws = EpisodeWorkspace(
        episode_id="pp_068",
        root_dir="/tmp/pp_068",
        assets=[asset],
        chapters=[Chapter(title="Intro", start_sec=0.0, end_sec=12.3)],
    )

    raw = ws.to_json()
    ws2 = EpisodeWorkspace.from_json(raw)
    assert ws.model_dump(mode="json") == ws2.model_dump(mode="json")


def test_invalid_verdict_rejected() -> None:
    with pytest.raises(ValidationError):
        ReviewIteration.model_validate({"iteration": 1, "verdict": "nope"})


def test_asset_ids_unique_in_workspace() -> None:
    with pytest.raises(ValidationError):
        EpisodeWorkspace(
            episode_id="pp_068",
            root_dir="/tmp/pp_068",
            assets=[Asset(asset_id="description"), Asset(asset_id="description")],
        )


def test_review_iterations_must_be_monotonic() -> None:
    with pytest.raises(ValidationError):
        Asset(
            asset_id="description",
            reviews=[
                ReviewIteration(iteration=2, verdict=ReviewVerdict.ok),
                ReviewIteration(iteration=1, verdict=ReviewVerdict.ok),
            ],
        )


def test_selected_candidate_must_exist() -> None:
    with pytest.raises(ValidationError):
        Asset(
            asset_id="description",
            candidates=[Candidate(asset_id="description", content="x")],
            selected_candidate_id=uuid4(),
        )


def test_verdict_ok_cannot_include_error_issues() -> None:
    with pytest.raises(ValidationError):
        ReviewIteration(
            iteration=1,
            verdict=ReviewVerdict.ok,
            issues=[ReviewIssue(message="bad")],
        )


def test_episode_yaml_roundtrip_with_hosts() -> None:
    from podcast_pipeline.domain.episode_yaml import EpisodeYaml

    yaml_data = EpisodeYaml(episode_id="ep_001", hosts=["Jochen", "Dominik"])
    dumped = yaml_data.to_mapping()
    restored = EpisodeYaml.model_validate(dumped)
    assert restored.hosts == ["Jochen", "Dominik"]
    assert restored.episode_id == "ep_001"


def test_episode_yaml_roundtrip_without_hosts() -> None:
    from podcast_pipeline.domain.episode_yaml import EpisodeYaml

    yaml_data = EpisodeYaml(episode_id="ep_001")
    dumped = yaml_data.to_mapping()
    restored = EpisodeYaml.model_validate(dumped)
    assert restored.hosts is None
    assert restored.episode_id == "ep_001"


def test_chapters_must_be_increasing() -> None:
    with pytest.raises(ValidationError):
        EpisodeWorkspace(
            episode_id="pp_068",
            root_dir="/tmp/pp_068",
            chapters=[
                Chapter(title="A", start_sec=10.0),
                Chapter(title="B", start_sec=10.0),
            ],
        )
