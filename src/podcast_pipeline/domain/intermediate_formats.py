from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic.types import AwareDatetime

from podcast_pipeline.domain.models import DomainModel, ProvenanceRef

ChunkId = Annotated[int, Field(ge=1)]


class LinkRef(DomainModel):
    url: Annotated[str, Field(min_length=1)]
    label: str | None = None


class TranscriptChunkMeta(DomainModel):
    """Metadata for an on-disk transcript chunk.

    The chunk text itself lives in a separate `.txt` file (diff-friendly).
    """

    version: Literal[1] = 1
    chunk_id: ChunkId
    text_relpath: Annotated[str, Field(min_length=1)]
    start_sec: Annotated[float, Field(ge=0)] | None = None
    end_sec: Annotated[float, Field(ge=0)] | None = None
    created_at: AwareDatetime | None = None
    provenance: list[ProvenanceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_time_range(self) -> TranscriptChunkMeta:
        if self.start_sec is not None and self.end_sec is not None and self.end_sec <= self.start_sec:
            raise ValueError("end_sec must be greater than start_sec when both are present")
        return self


class ChunkSummary(DomainModel):
    """Structured summary of a single transcript chunk."""

    version: Literal[1] = 1
    chunk_id: ChunkId
    summary_markdown: str
    bullets: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    links: list[LinkRef] = Field(default_factory=list)
    created_at: AwareDatetime | None = None
    provenance: list[ProvenanceRef] = Field(default_factory=list)


class EpisodeSummary(DomainModel):
    """Structured roll-up derived from all chunk summaries."""

    version: Literal[1] = 1
    summary_markdown: str
    key_points: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    links: list[LinkRef] = Field(default_factory=list)
    created_at: AwareDatetime | None = None
    provenance: list[ProvenanceRef] = Field(default_factory=list)
