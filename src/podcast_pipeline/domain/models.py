from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Generic, Self, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.types import AwareDatetime

AssetId = Annotated[str, Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")]
TrackId = Annotated[str, Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SchemaVersioned(DomainModel):
    schema_version: Annotated[int, Field(ge=1)] = 1
    _schema_version: ClassVar[int] = 1

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version != self._schema_version:
            raise ValueError(
                f"Unsupported schema_version {self.schema_version}; expected {self._schema_version}",
            )
        return self


class TextFormat(StrEnum):
    markdown = "markdown"
    plain = "plain"
    html = "html"


class AssetKind(StrEnum):
    description = "description"
    shownotes = "shownotes"
    summary_short = "summary_short"
    title_detail = "title_detail"
    title_seo = "title_seo"
    subtitle_auphonic = "subtitle_auphonic"
    slug = "slug"
    cms_tags = "cms_tags"
    audio_tags = "audio_tags"
    itunes_keywords = "itunes_keywords"
    mastodon = "mastodon"
    linkedin = "linkedin"
    youtube_description = "youtube_description"


class ReviewVerdict(StrEnum):
    ok = "ok"
    changes_requested = "changes_requested"
    needs_human = "needs_human"


class IssueSeverity(StrEnum):
    error = "error"
    warning = "warning"


class ProvenanceRef(DomainModel):
    kind: Annotated[str, Field(min_length=1)]
    ref: Annotated[str, Field(min_length=1)]
    created_at: AwareDatetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Candidate(DomainModel):
    candidate_id: UUID = Field(default_factory=uuid4)
    asset_id: AssetId
    format: TextFormat = TextFormat.markdown
    content: str
    created_at: AwareDatetime = Field(default_factory=_utc_now)
    provenance: list[ProvenanceRef] = Field(default_factory=list)


class ReviewIssue(DomainModel):
    issue_id: UUID = Field(default_factory=uuid4)
    severity: IssueSeverity = IssueSeverity.error
    message: str
    code: str | None = None
    field: str | None = None


class ReviewIteration(DomainModel):
    iteration: Annotated[int, Field(ge=1)]
    verdict: ReviewVerdict
    issues: list[ReviewIssue] = Field(default_factory=list)
    reviewer: str | None = None
    created_at: AwareDatetime = Field(default_factory=_utc_now)
    summary: str | None = None
    provenance: list[ProvenanceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_verdict_issues(self) -> ReviewIteration:
        if self.verdict == ReviewVerdict.ok:
            error_issues = [issue for issue in self.issues if issue.severity == IssueSeverity.error]
            if error_issues:
                raise ValueError("verdict=ok cannot include severity=error issues")
        return self


class Asset(DomainModel):
    asset_id: AssetId
    kind: AssetKind | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    reviews: list[ReviewIteration] = Field(default_factory=list)
    selected_candidate_id: UUID | None = None

    @model_validator(mode="after")
    def _validate_relations(self) -> Asset:
        if self.kind is not None and self.kind.value != self.asset_id:
            raise ValueError("asset.kind must match asset.asset_id when provided")

        candidate_ids: set[UUID] = set()
        for candidate in self.candidates:
            if candidate.asset_id != self.asset_id:
                raise ValueError("candidate.asset_id must match asset.asset_id")
            if candidate.candidate_id in candidate_ids:
                raise ValueError("candidate_id must be unique within an asset")
            candidate_ids.add(candidate.candidate_id)

        last_iteration = 0
        for review in self.reviews:
            if review.iteration <= last_iteration:
                raise ValueError("review iterations must be strictly monotonic increasing")
            last_iteration = review.iteration

        if self.selected_candidate_id is not None and self.selected_candidate_id not in candidate_ids:
            raise ValueError("selected_candidate_id must refer to an existing candidate")

        return self


class Chapter(DomainModel):
    title: str
    start_sec: Annotated[float, Field(ge=0)]
    end_sec: Annotated[float, Field(ge=0)] | None = None
    summary: str | None = None

    @model_validator(mode="after")
    def _validate_times(self) -> Chapter:
        if self.end_sec is not None and self.end_sec <= self.start_sec:
            raise ValueError("end_sec must be greater than start_sec")
        return self


class Track(DomainModel):
    track_id: TrackId
    path: str
    label: str | None = None
    role: str | None = None
    provenance: list[ProvenanceRef] = Field(default_factory=list)


class EpisodeWorkspace(SchemaVersioned):
    episode_id: Annotated[str, Field(min_length=1)]
    root_dir: Annotated[str, Field(min_length=1)]
    assets: list[Asset] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    tracks: list[Track] = Field(default_factory=list)
    created_at: AwareDatetime = Field(default_factory=_utc_now)
    provenance: list[ProvenanceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_invariants(self) -> EpisodeWorkspace:
        asset_ids: set[str] = set()
        for asset in self.assets:
            if asset.asset_id in asset_ids:
                raise ValueError("asset_id must be unique within a workspace")
            asset_ids.add(asset.asset_id)

        track_ids: set[str] = set()
        for track in self.tracks:
            if track.track_id in track_ids:
                raise ValueError("track_id must be unique within a workspace")
            track_ids.add(track.track_id)

        last_start = -1.0
        for chapter in self.chapters:
            if chapter.start_sec <= last_start:
                raise ValueError("chapters must have strictly increasing start_sec")
            last_start = chapter.start_sec

        return self

    def to_json(self, *, indent: int | None = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, raw: str) -> EpisodeWorkspace:
        return cls.model_validate_json(raw)


T = TypeVar("T")


@dataclass(frozen=True)
class LoadResult(Generic[T]):
    value: T | None
    error: str | None


def try_load_workspace_json(raw: str) -> LoadResult[EpisodeWorkspace]:
    try:
        return LoadResult(value=EpisodeWorkspace.from_json(raw), error=None)
    except ValidationError as exc:
        return LoadResult(value=None, error=str(exc))
