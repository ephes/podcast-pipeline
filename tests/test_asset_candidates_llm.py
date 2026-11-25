from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

from podcast_pipeline.asset_candidates_llm import generate_draft_candidates_llm, generate_single_asset_candidates_llm
from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.domain.models import AssetKind
from podcast_pipeline.prompting import PromptRenderer, default_prompt_registry
from podcast_pipeline.tag_assets import TAG_ASSET_IDS, render_tags
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


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
                {
                    "asset_id": asset_id,
                    "content": "python-315" if kind == AssetKind.slug else f"{asset_id} candidate 1",
                },
                {
                    "asset_id": asset_id,
                    "content": "python-316" if kind == AssetKind.slug else f"{asset_id} candidate 2",
                },
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
                {"asset_id": asset_id, "content": "python-315" if kind == AssetKind.slug else f"{asset_id} only"},
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


def test_generate_draft_candidates_llm_shownotes_prompt_requires_links() -> None:
    episode_summary = _make_episode_summary()
    responses = {
        kind.value: {
            "candidates": [
                {"asset_id": kind.value, "content": "python-315" if kind == AssetKind.slug else "candidate 1"},
            ],
        }
        for kind in AssetKind
    }
    runner = FakeDrafterRunner(responses)
    renderer = PromptRenderer(default_prompt_registry())

    generate_draft_candidates_llm(
        episode_summary=episode_summary,
        chapters=["00:00 Intro"],
        candidates_per_asset=1,
        runner=runner,
        renderer=renderer,
    )

    shownotes_prompt = next(prompt for prompt in runner.prompts if "Asset type: shownotes" in prompt)
    assert "link-first shownotes" in shownotes_prompt
    assert "section '## Links'" in shownotes_prompt
    assert "many concrete markdown links" in shownotes_prompt


def test_drafting_owns_model_candidate_identity_and_origin() -> None:
    existing_id = uuid4()
    responses = {
        kind.value: {
            "candidates": [
                {
                    "candidate_id": str(existing_id),
                    "content": "alternative-a" if kind == AssetKind.slug else "Alternative A",
                    "created_at": "2099-01-01T00:00:00Z",
                    "provenance": [{"kind": "producer", "ref": "forged-origin"}],
                },
                {
                    "candidate_id": str(existing_id),
                    "content": "alternative-b" if kind == AssetKind.slug else "Alternative B",
                    "created_at": "2099-01-01T00:00:00Z",
                    "provenance": [],
                },
            ]
        }
        for kind in AssetKind
    }
    before = datetime.now(UTC)
    assets = generate_draft_candidates_llm(
        episode_summary=_make_episode_summary(),
        chapters=[],
        candidates_per_asset=2,
        runner=FakeDrafterRunner(responses),
        renderer=PromptRenderer(default_prompt_registry()),
    )
    after = datetime.now(UTC)
    candidates = [c for alternatives in assets.values() for c in alternatives]
    assert existing_id not in {c.candidate_id for c in candidates}
    assert len({c.candidate_id for c in candidates}) == 2 * len(AssetKind)
    for asset_id, alternatives in assets.items():
        for idx, candidate in enumerate(alternatives, start=1):
            assert before <= candidate.created_at <= after
            (origin,) = candidate.provenance
            assert (origin.kind, origin.ref, origin.created_at) == (
                "llm_drafter",
                f"asset_v1:{idx:02d}",
                candidate.created_at,
            )
        expected = ["alternative-a", "alternative-b"] if asset_id == "slug" else ["Alternative A", "Alternative B"]
        if asset_id in TAG_ASSET_IDS:
            expected = [render_tags(asset_id, [value]) for value in expected]
        assert [c.content for c in alternatives] == expected


@pytest.mark.parametrize(
    ("summary_file", "error"),
    [
        ("valid", None),
        ("missing", "Episode summary not found. Run the draft pipeline first."),
        ("oversized", "Invalid summary"),
        ("symlink", "Invalid summary"),
        ("duplicate", "Invalid or excessive summary JSON evidence"),
    ],
)
def test_llm_regeneration_admits_required_summary_before_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, summary_file: str, error: str | None
) -> None:
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(tmp_path / "missing-config.yaml"))
    store = EpisodeWorkspaceStore(tmp_path / "episode")
    store.write_episode_yaml({"episode_id": "synthetic"})
    summary = store.layout.episode_summary_json_path()
    summary.parent.mkdir(parents=True)
    text = '{"summary_markdown":"Synthetic summary"}'
    if summary_file == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_text(text, encoding="utf-8")
        summary.symlink_to(outside)
    elif summary_file == "oversized":
        summary.write_text(text + " " * (1024 * 1024), encoding="utf-8")
    elif summary_file == "duplicate":
        summary.write_text(
            '{"summary_markdown":"First summary","summary_markdown":"Synthetic summary"}', encoding="utf-8"
        )
    elif summary_file == "valid":
        summary.write_text(text, encoding="utf-8")

    runner = Mock(return_value={"candidates": [{"content": "Synthetic copy"}]})
    monkeypatch.setattr("podcast_pipeline.drafter_runner.DrafterCliRunner", Mock(return_value=Mock(run=runner)))
    if error is not None:
        with pytest.raises(RuntimeError, match=error):
            generate_single_asset_candidates_llm(
                workspace=store.layout.root, asset_id="description", candidates_per_asset=1
            )
        runner.assert_not_called()
    else:
        candidates = generate_single_asset_candidates_llm(
            workspace=store.layout.root, asset_id="description", candidates_per_asset=1
        )
        assert candidates[0].content == "Synthetic copy"
        runner.assert_called_once()
        assert "Synthetic summary" in runner.call_args.args[0]
