from __future__ import annotations

from typing import Any

import pytest

from podcast_pipeline.asset_candidates_llm import generate_draft_candidates_llm
from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.domain.models import AssetKind
from podcast_pipeline.prompting import PromptRenderer, default_prompt_registry


class FakeDrafterRunner:
    """Test double that returns scripted JSON responses keyed by asset_id."""

    def __init__(self, responses_by_asset: dict[str, dict[str, Any]]) -> None:
        self._responses = responses_by_asset
        self.prompts: list[str] = []
        self._call_count = 0

    def run(self, prompt_text: str) -> dict[str, Any]:
        self.prompts.append(prompt_text)
        # Extract asset_id from the prompt to select the right response
        for asset_id, response in self._responses.items():
            if f"Asset type: {asset_id}" in prompt_text:
                return dict(response)
        # Fallback: return a generic candidates response
        self._call_count += 1
        return {
            "candidates": [
                {"asset_id": "unknown", "content": f"fallback content {self._call_count}"},
            ],
        }


def _make_episode_summary() -> EpisodeSummary:
    return EpisodeSummary(
        summary_markdown="# Test Episode\n\nTest summary\n",
        key_points=["Point A", "Point B"],
        topics=["Topic 1", "Topic 2"],
    )


def test_generate_draft_candidates_llm_all_assets() -> None:
    episode_summary = _make_episode_summary()
    num_candidates = 2

    # Build scripted responses for every asset type
    responses: dict[str, dict[str, Any]] = {}
    for kind in AssetKind:
        asset_id = kind.value
        responses[asset_id] = {
            "candidates": [
                {"asset_id": asset_id, "content": f"{asset_id} candidate 1"},
                {"asset_id": asset_id, "content": f"{asset_id} candidate 2"},
            ],
        }

    runner = FakeDrafterRunner(responses)
    renderer = PromptRenderer(default_prompt_registry())

    assets = generate_draft_candidates_llm(
        episode_summary=episode_summary,
        chapters=["Chapter 1", "Chapter 2"],
        candidates_per_asset=num_candidates,
        runner=runner,
        renderer=renderer,
    )

    # Should have all 13 asset types
    assert len(assets) == len(AssetKind)

    for kind in AssetKind:
        asset_id = kind.value
        assert asset_id in assets
        candidates = assets[asset_id]
        assert len(candidates) == num_candidates
        for candidate in candidates:
            assert candidate.asset_id == asset_id
            assert candidate.content

    # One prompt per asset type
    assert len(runner.prompts) == len(AssetKind)


def test_generate_draft_candidates_llm_single_candidate() -> None:
    episode_summary = _make_episode_summary()

    responses: dict[str, dict[str, Any]] = {}
    for kind in AssetKind:
        asset_id = kind.value
        responses[asset_id] = {
            "candidates": [
                {"asset_id": asset_id, "content": f"{asset_id} only"},
            ],
        }

    runner = FakeDrafterRunner(responses)
    renderer = PromptRenderer(default_prompt_registry())

    assets = generate_draft_candidates_llm(
        episode_summary=episode_summary,
        chapters=[],
        candidates_per_asset=1,
        runner=runner,
        renderer=renderer,
    )

    for kind in AssetKind:
        assert len(assets[kind.value]) == 1


def test_generate_draft_candidates_llm_rejects_wrong_asset_id() -> None:
    episode_summary = _make_episode_summary()

    # Return wrong asset_id for description
    responses: dict[str, dict[str, Any]] = {}
    for kind in AssetKind:
        asset_id = kind.value
        if asset_id == "description":
            responses[asset_id] = {
                "candidates": [
                    {"asset_id": "wrong_id", "content": "bad candidate"},
                ],
            }
        else:
            responses[asset_id] = {
                "candidates": [
                    {"asset_id": asset_id, "content": f"{asset_id} ok"},
                ],
            }

    runner = FakeDrafterRunner(responses)
    renderer = PromptRenderer(default_prompt_registry())

    with pytest.raises(RuntimeError, match="wrong asset_id"):
        generate_draft_candidates_llm(
            episode_summary=episode_summary,
            chapters=[],
            candidates_per_asset=1,
            runner=runner,
            renderer=renderer,
        )


def test_generate_draft_candidates_llm_rejects_missing_candidates_array() -> None:
    episode_summary = _make_episode_summary()

    class BadRunner:
        def run(self, prompt_text: str) -> dict[str, Any]:
            return {"not_candidates": []}

    runner = BadRunner()
    renderer = PromptRenderer(default_prompt_registry())

    with pytest.raises(RuntimeError, match="missing 'candidates' array"):
        generate_draft_candidates_llm(
            episode_summary=episode_summary,
            chapters=[],
            candidates_per_asset=1,
            runner=runner,
            renderer=renderer,
        )


def test_generate_draft_candidates_llm_rejects_wrong_count() -> None:
    episode_summary = _make_episode_summary()

    # Return 1 candidate when 2 are requested
    responses: dict[str, dict[str, Any]] = {}
    for kind in AssetKind:
        asset_id = kind.value
        responses[asset_id] = {
            "candidates": [
                {"asset_id": asset_id, "content": "only one"},
            ],
        }

    runner = FakeDrafterRunner(responses)
    renderer = PromptRenderer(default_prompt_registry())

    with pytest.raises(RuntimeError, match="expected 2"):
        generate_draft_candidates_llm(
            episode_summary=episode_summary,
            chapters=[],
            candidates_per_asset=2,
            runner=runner,
            renderer=renderer,
        )
