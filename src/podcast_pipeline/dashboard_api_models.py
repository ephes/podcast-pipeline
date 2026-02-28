from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _DashboardApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssetCandidateOut(_DashboardApiModel):
    candidate_id: str
    content: str
    content_html: str
    format: str
    tags: list[str] | None = None


class AssetOut(_DashboardApiModel):
    asset_id: str
    selected_candidate_id: str | None = None
    has_selection: bool
    candidates: list[AssetCandidateOut]
    selected_tags: list[str] | None = None


class AssetTagsOut(_DashboardApiModel):
    asset_id: str
    tags: list[str]


class AssetNotesOut(_DashboardApiModel):
    asset_id: str
    notes: str


class StatusStagesOut(_DashboardApiModel):
    episode_yaml: bool
    state_json: bool
    transcript: bool
    chunks: int
    summary: bool
    candidates: int
    candidate_assets: int
    selected: int
    total_assets: int


class StatusOut(_DashboardApiModel):
    workspace: str
    episode_id: str
    hosts: list[str]
    stages: StatusStagesOut
