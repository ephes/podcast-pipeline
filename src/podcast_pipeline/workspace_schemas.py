from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from podcast_pipeline.domain.episode_yaml import EpisodeYaml
from podcast_pipeline.domain.models import EpisodeWorkspace

__all__ = [
    "episode_yaml_schema",
    "state_json_schema",
    "validate_episode_yaml_payload",
    "validate_state_payload",
    "parse_state_json",
]


def episode_yaml_schema() -> dict[str, Any]:
    """JSON schema for episode.yaml workspace metadata."""
    return EpisodeYaml.model_json_schema()


def state_json_schema() -> dict[str, Any]:
    """JSON schema for state.json workspace snapshots."""
    return EpisodeWorkspace.model_json_schema()


def validate_episode_yaml_payload(payload: Mapping[str, Any]) -> EpisodeYaml:
    return EpisodeYaml.model_validate(payload)


def validate_state_payload(payload: Mapping[str, Any]) -> EpisodeWorkspace:
    return EpisodeWorkspace.model_validate(payload)


def parse_state_json(raw: str | bytes | bytearray) -> EpisodeWorkspace:
    return EpisodeWorkspace.model_validate_json(raw)
