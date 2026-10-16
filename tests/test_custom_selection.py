from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from podcast_pipeline.agent_runners import FakeCreatorRunner, FakeReviewerRunner
from podcast_pipeline.auphonic_payload import build_auphonic_payload
from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace, ReviewVerdict, TextFormat
from podcast_pipeline.entrypoints.cli import app
from podcast_pipeline.pick_core import (
    MANUAL_SELECTION_PROVENANCE_KIND,
    build_asset,
    clear_manual_selection_provenance,
    custom_selection_format,
    has_manual_selection_provenance,
    normalize_custom_selection_text,
    read_current_selection,
    save_custom_selection,
    suggest_slug,
    update_workspace_assets,
    validate_slug,
)
from podcast_pipeline.review_loop_orchestrator import run_review_loop_orchestrator
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def _store(tmp_path: Path) -> EpisodeWorkspaceStore:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep_001"})
    return store


@pytest.mark.parametrize(
    ("asset_id", "expected"),
    [
        ("slug", TextFormat.plain),
        ("title_detail", TextFormat.plain),
        ("title_seo", TextFormat.plain),
        ("subtitle_auphonic", TextFormat.plain),
        ("summary_short", TextFormat.plain),
        ("description", TextFormat.markdown),
        ("shownotes", TextFormat.markdown),
        ("cms_tags", TextFormat.markdown),
        ("mastodon", TextFormat.markdown),
    ],
)
def test_custom_selection_format(asset_id: str, expected: TextFormat) -> None:
    assert custom_selection_format(asset_id) == expected


@pytest.mark.parametrize("slug", ["python-315", "python-3-15", "pp72", "a"])
def test_validate_slug_accepts_url_safe_slugs(slug: str) -> None:
    validate_slug(slug)


@pytest.mark.parametrize("slug", ["Python-315", "python 3.15", "python--315", "-python", "python-", "über", ""])
def test_validate_slug_rejects_invalid_slugs(slug: str) -> None:
    with pytest.raises(ValueError, match="Invalid slug"):
        validate_slug(slug)


def test_validate_slug_error_suggests_a_valid_slug() -> None:
    with pytest.raises(ValueError, match="'python-3-15'"):
        validate_slug("Python 3.15")


def test_suggest_slug_transliterates_german() -> None:
    assert suggest_slug("Größere Übungen für Python 3.15!") == "groessere-uebungen-fuer-python-3-15"


def test_normalize_single_line_asset_strips_and_rejects_newlines() -> None:
    assert normalize_custom_selection_text("title_detail", "  Python 3.15  \n") == "Python 3.15"
    with pytest.raises(ValueError, match="single line"):
        normalize_custom_selection_text("title_detail", "Line one\nLine two")


def test_normalize_markdown_keeps_structure_and_line_endings() -> None:
    text = "\r\n# Heading\r\n\r\n  - indented item\r\n\r\n"
    assert normalize_custom_selection_text("description", text) == "# Heading\n\n  - indented item"


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_normalize_rejects_empty_text(text: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        normalize_custom_selection_text("description", text)


def test_save_custom_slug_writes_plain_selection_and_records_provenance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate = Candidate(asset_id="slug", content="python-neuigkeiten")
    store.write_candidate(candidate)
    workspace = EpisodeWorkspace(episode_id="ep_001", root_dir=".")

    updated = save_custom_selection(
        store=store,
        workspace=workspace,
        asset_id="slug",
        candidates=[candidate],
        text=" python-315\n",
        based_on=candidate,
    )

    assert store.layout.selected_text_path("slug", TextFormat.plain).read_text(encoding="utf-8") == "python-315\n"
    assert not store.layout.selected_text_path("slug", TextFormat.markdown).exists()
    assert read_current_selection(store.layout, "slug") == (TextFormat.plain, "python-315\n")

    state = store.read_state()
    assert state == updated
    asset = next(asset for asset in state.assets if asset.asset_id == "slug")
    assert asset.selected_candidate_id is None
    assert [c.candidate_id for c in asset.candidates] == [candidate.candidate_id]
    assert has_manual_selection_provenance(state, "slug")

    record_path = store.layout.provenance_json_path(MANUAL_SELECTION_PROVENANCE_KIND, "slug")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["based_on_candidate_id"] == str(candidate.candidate_id)
    assert record["edited"] is True
    assert record["format"] == "plain"
    assert "created_at" in record


def test_save_custom_selection_rejects_invalid_slug_without_writing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate = Candidate(asset_id="slug", content="python-neuigkeiten")
    workspace = EpisodeWorkspace(episode_id="ep_001", root_dir=".")

    with pytest.raises(ValueError, match="Invalid slug"):
        save_custom_selection(
            store=store,
            workspace=workspace,
            asset_id="slug",
            candidates=[candidate],
            text="Python 3.15",
        )

    assert read_current_selection(store.layout, "slug") is None
    assert not store.layout.state_json.exists()


def test_save_custom_markdown_replaces_picked_candidate_and_derives_html(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate = Candidate(asset_id="description", content="# Picked\n\nOld text.")
    store.write_candidate(candidate)
    store.write_selected_text("description", TextFormat.markdown, candidate.content)
    picked = update_workspace_assets(
        EpisodeWorkspace(episode_id="ep_001", root_dir="."),
        {
            "description": build_asset(
                asset_id="description",
                existing=None,
                candidates=[candidate],
                selected_candidate_id=candidate.candidate_id,
            )
        },
    )
    store.write_state(picked)

    updated = save_custom_selection(
        store=store,
        workspace=picked,
        asset_id="description",
        candidates=[candidate],
        text="# Mine\n\nWith a [link](https://example.com).",
    )

    md = store.layout.selected_text_path("description", TextFormat.markdown).read_text(encoding="utf-8")
    html = store.layout.selected_text_path("description", TextFormat.html).read_text(encoding="utf-8")
    assert md == "# Mine\n\nWith a [link](https://example.com).\n"
    assert '<a href="https://example.com">link</a>' in html
    assert updated.assets[0].selected_candidate_id is None

    # Saving again replaces (not duplicates) the provenance marker; picking a candidate drops it.
    again = save_custom_selection(
        store=store,
        workspace=updated,
        asset_id="description",
        candidates=[candidate],
        text="# Mine v2",
    )
    manual_refs = [ref for ref in again.provenance if ref.kind == MANUAL_SELECTION_PROVENANCE_KIND]
    assert len(manual_refs) == 1
    assert not has_manual_selection_provenance(clear_manual_selection_provenance(again, "description"), "description")


def test_status_counts_custom_selection_as_selected(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    result = runner.invoke(app, ["review", "--fake-runner", "--workspace", str(workspace)])
    assert result.exit_code == 0, result.stdout

    store = EpisodeWorkspaceStore(workspace)
    store.clear_selected_text("description")
    state = store.read_state()
    asset = next(asset for asset in state.assets if asset.asset_id == "description")
    save_custom_selection(
        store=store,
        workspace=state,
        asset_id="description",
        candidates=list(asset.candidates),
        text="# Hand-written description",
    )

    result = runner.invoke(app, ["status", "--workspace", str(workspace)])
    assert result.exit_code == 0, result.stdout
    selection_line = next(line for line in result.stdout.splitlines() if "copy/selected" in line)
    assert re.search(r"(?<!\w)description(?!\w)", selection_line) is None, selection_line


def test_produce_payload_uses_custom_plain_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    config: dict[str, Any] = {"auphonic": {"presets": {"main": "preset_123"}}}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(config_path))
    workspace = tmp_path / "workspace"
    store = EpisodeWorkspaceStore(workspace)
    candidate = Candidate(asset_id="title_detail", content="# Title\n\nGenerated title")
    save_custom_selection(
        store=store,
        workspace=EpisodeWorkspace(episode_id="ep_001", root_dir="."),
        asset_id="title_detail",
        candidates=[candidate],
        text="Python 3.15: Was ist neu?",
    )

    payload = build_auphonic_payload(
        # One speaker stem keeps the payload valid for multitrack productions too.
        episode_yaml={
            "episode_id": "ep_001",
            "auphonic": {"preset": "main", "tracks": [{"id": "host", "path": "stems/host.flac"}]},
        },
        workspace=workspace,
    )

    assert payload["metadata"]["title"] == "Python 3.15: Was ist neu?"


def _run_slug_review(store: EpisodeWorkspaceStore, *, creator_content: str) -> ReviewVerdict:
    creator = FakeCreatorRunner(
        layout=store.layout,
        replies=[{"done": True, "candidate": {"content": creator_content}}],
    )
    reviewer = FakeReviewerRunner(layout=store.layout, reviewer="reviewer_a", replies=[{"verdict": "ok"}])
    run_review_loop_orchestrator(
        workspace=store.layout.root,
        asset_id="slug",
        max_iterations=1,
        creator=creator,
        reviewer=reviewer,
    )
    return store.read_review("slug", 1, reviewer="reviewer_a").verdict


def test_review_loop_keeps_custom_plain_slug_locked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    seed = Candidate(asset_id="slug", content="# Slug\n\npython-neuigkeiten\n")
    store.write_candidate(seed)
    state = save_custom_selection(
        store=store,
        workspace=EpisodeWorkspace(episode_id="ep_001", root_dir="."),
        asset_id="slug",
        candidates=[seed],
        text="python-315",
    )
    assert has_manual_selection_provenance(state, "slug")

    # The creator's markdown candidate differs from the plain custom slug, so the lock must trigger.
    assert _run_slug_review(store, creator_content="# Slug\n\nsomething-else\n") == ReviewVerdict.changes_requested
    assert store.layout.selected_text_path("slug", TextFormat.plain).read_text(encoding="utf-8") == "python-315\n"


def test_review_loop_convergence_drops_manual_selection_marker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    seed = Candidate(asset_id="description", content="seed")
    store.write_candidate(seed)
    save_custom_selection(
        store=store,
        workspace=EpisodeWorkspace(episode_id="ep_001", root_dir="."),
        asset_id="description",
        candidates=[seed],
        text="# Custom",
    )

    creator = FakeCreatorRunner(layout=store.layout, replies=[{"done": True, "candidate": {"content": "# Reviewed"}}])
    reviewer = FakeReviewerRunner(layout=store.layout, reviewer="reviewer_a", replies=[{"verdict": "ok"}])
    run_review_loop_orchestrator(
        workspace=store.layout.root,
        asset_id="description",
        max_iterations=1,
        creator=creator,
        reviewer=reviewer,
    )

    state = store.read_state()
    asset = next(asset for asset in state.assets if asset.asset_id == "description")
    assert asset.selected_candidate_id is not None
    assert not has_manual_selection_provenance(state, "description")
