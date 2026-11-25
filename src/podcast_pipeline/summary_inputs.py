"""Admission of the persisted episode summary for consumers of the standalone artifact."""

from __future__ import annotations

from typing import TYPE_CHECKING

from podcast_pipeline.control_json import load_control_json
from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.file_inputs import read_workspace_utf8

if TYPE_CHECKING:
    from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout

MAX_SUMMARY_BYTES = 1024 * 1024


class EpisodeSummaryInputError(ValueError):
    """The summary exists but cannot be read or validated safely."""


def load_workspace_episode_summary(layout: EpisodeWorkspaceLayout) -> EpisodeSummary:
    """Read a bounded summary snapshot, allowing callers to handle absence separately."""
    summary_path = layout.episode_summary_json_path()
    try:
        text = read_workspace_utf8(layout.root, summary_path.relative_to(layout.root), max_bytes=MAX_SUMMARY_BYTES)
    except FileNotFoundError:
        raise
    except (OSError, ValueError):
        raise EpisodeSummaryInputError(
            "Invalid summary: require coherent regular workspace UTF-8 input within 1 MiB"
        ) from None
    try:
        return EpisodeSummary.model_validate(load_control_json(text))
    except ValueError:
        raise EpisodeSummaryInputError("Invalid or excessive summary JSON evidence") from None
