from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from math import isfinite
from typing import Any

from pydantic import ConfigDict, Field, ValidationError

from podcast_pipeline.domain.models import DomainModel, LoadResult, SchemaVersioned, Track


class EpisodeInputs(DomainModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    transcript: str | None = None
    chapters: str | None = None
    transcription_audio: str | None = None


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
    editorial_notes: dict[str, str] | None = None

    def to_mapping(self, *, exclude_unset: bool = False) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True, exclude_unset=exclude_unset)
        # JSON-mode dumping turns NaN/Infinity extras into null. Preserve
        # chapter data and nonfinite offsets for the Auphonic validators.
        auphonic = (self.model_extra or {}).get("auphonic")
        if isinstance(auphonic, Mapping):
            if "chapters" in auphonic:
                payload["auphonic"]["chapters"] = deepcopy(auphonic["chapters"])
            tracks = auphonic.get("tracks")
            if isinstance(tracks, list):
                for raw_track, dumped_track in zip(tracks, payload["auphonic"]["tracks"], strict=True):
                    offset = raw_track.get("offset") if isinstance(raw_track, Mapping) else None
                    if isinstance(offset, float) and not isfinite(offset):
                        dumped_track["offset"] = offset
        payload["schema_version"] = self.schema_version
        return payload


def try_load_episode_yaml(raw: Mapping[str, Any]) -> LoadResult[EpisodeYaml]:
    try:
        return LoadResult(value=EpisodeYaml.model_validate(raw), error=None)
    except ValidationError as exc:
        return LoadResult(value=None, error=str(exc))
