from __future__ import annotations

import pytest
from pydantic import ValidationError

from podcast_pipeline.domain.episode_yaml import EpisodeYaml
from podcast_pipeline.domain.models import EpisodeWorkspace


def test_episode_yaml_rejects_schema_version_mismatch() -> None:
    with pytest.raises(ValidationError):
        EpisodeYaml.model_validate({"episode_id": "ep_001", "schema_version": 2})


def test_workspace_rejects_schema_version_mismatch() -> None:
    with pytest.raises(ValidationError):
        EpisodeWorkspace.model_validate(
            {"episode_id": "ep_001", "root_dir": ".", "schema_version": 2},
        )
