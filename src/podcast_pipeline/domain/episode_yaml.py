from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ConfigDict, Field, ValidationError

from podcast_pipeline.domain.models import DomainModel, LoadResult, SchemaVersioned, Track


class EpisodeInputs(DomainModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    transcript: str | None = None
    chapters: str | None = None


class EpisodeSources(DomainModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    reaper_media_dir: str | None = None
    tracks_glob: str | None = None


class EpisodeAgentConfig(DomainModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    command: str | None = None
    args: list[str] | None = None
    kind: str | None = None
    install_hint: str | None = None
    check_command: str | None = None


class EpisodeAgents(DomainModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    creator: EpisodeAgentConfig | None = None
    reviewer: EpisodeAgentConfig | None = None


class EpisodeTrack(Track):
    model_config = ConfigDict(extra="allow", validate_assignment=True)


class EpisodeYaml(SchemaVersioned):
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    episode_id: str = Field(min_length=1)
    hosts: list[str] | None = None
    inputs: EpisodeInputs = Field(default_factory=EpisodeInputs)
    sources: EpisodeSources | None = None
    tracks: list[EpisodeTrack] = Field(default_factory=list)
    agents: EpisodeAgents | None = None

    def to_mapping(self, *, exclude_unset: bool = False) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True, exclude_unset=exclude_unset)
        payload["schema_version"] = self.schema_version
        return payload


def try_load_episode_yaml(raw: Mapping[str, Any]) -> LoadResult[EpisodeYaml]:
    try:
        return LoadResult(value=EpisodeYaml.model_validate(raw), error=None)
    except ValidationError as exc:
        return LoadResult(value=None, error=str(exc))
