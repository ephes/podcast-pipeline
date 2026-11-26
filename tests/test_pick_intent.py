from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from podcast_pipeline import pick_core
from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace, TextFormat
from podcast_pipeline.entrypoints import pick
from podcast_pipeline.entrypoints.cli import app
from podcast_pipeline.pick_core import save_candidate_selection
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def _candidates(store: EpisodeWorkspaceStore, asset_id: str) -> list[Candidate]:
    candidates = [Candidate(asset_id=asset_id, content=f"Synthetic candidate {number}") for number in (1, 2)]
    for candidate in candidates:
        store.write_candidate(candidate)
    return candidates


@pytest.mark.parametrize("asset_filter", [None, "description"])
@pytest.mark.parametrize("limit", ["directory entries", "candidate JSON bytes"])
def test_picker_refuses_over_budget_catalog_before_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asset_filter: str | None, limit: str
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    candidate = Candidate(asset_id="description", content="Synthetic first candidate")
    first = store.write_candidate(candidate)
    store.write_state(EpisodeWorkspace(episode_id="synthetic", root_dir="."))
    selected = store.write_selected_text("description", TextFormat.markdown, "Original text")
    preserved = {path: path.read_bytes() for path in (store.layout.state_json, selected)}

    if limit == "directory entries":
        directory = first.parent if asset_filter else store.layout.copy_candidates_dir
        for index in range(12):
            (directory / f"unrelated_{index}.txt").touch()
        monkeypatch.setattr(pick_core, "MAX_CATALOG_ENTRIES", 10, raising=False)
    else:
        asset_id = "description" if asset_filter else "shownotes"
        extra = [
            store.write_candidate(Candidate(asset_id=asset_id, content=f"Synthetic candidate {index}"))
            for index in range(12)
        ]
        budget = first.stat().st_size + max(path.stat().st_size for path in extra)
        monkeypatch.setattr(pick_core, "MAX_CATALOG_BYTES", budget, raising=False)

    args = ["pick", "--workspace", str(tmp_path)]
    if asset_filter:
        args += ["--asset-id", asset_filter, "--candidate-id", str(candidate.candidate_id)]
    result = CliRunner().invoke(app, args, input="1\n")

    assert result.exit_code != 0, result.output
    assert "Picker catalog exceeds" in result.output
    assert limit in result.output
    assert "Workspace:" not in result.output
    assert {path: path.read_bytes() for path in preserved} == preserved


@pytest.mark.parametrize("change", ["replace", "delete", "same_bytes_new_inode", "changed_json_bytes"])
def test_interactive_pick_refuses_changed_displayed_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    candidates = _candidates(store, "description")
    previous = save_candidate_selection(
        store=store,
        workspace=EpisodeWorkspace(episode_id="synthetic", root_dir="."),
        candidate=candidates[0],
        candidates=candidates,
        selected_format=candidates[0].format,
        selected_text=candidates[0].content,
    )
    state_before = store.layout.state_json.read_bytes()
    selected = store.layout.selected_text_path("description", candidates[0].format)
    text_before = selected.read_bytes()
    path = store.layout.candidate_json_path("description", candidates[1].candidate_id)

    def prompt(*args: Any, **kwargs: Any) -> int:
        if change == "replace":
            store.write_candidate(candidates[1].model_copy(update={"content": "Regenerated synthetic copy"}))
        elif change == "delete":
            path.unlink()
        elif change == "same_bytes_new_inode":
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
        else:
            path.write_bytes(path.read_bytes() + b"\n")
        return 2

    monkeypatch.setattr(typer, "prompt", prompt)
    result = CliRunner().invoke(app, ["pick", "--workspace", str(tmp_path)])
    assert result.exit_code != 0, result.output
    assert "reload" in result.output.lower()
    assert "Selected description:" not in result.output
    assert store.layout.state_json.read_bytes() == state_before
    assert selected.read_bytes() == text_before
    assert store.read_state() == previous


def test_pick_rechecks_after_entering_update_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    candidate = _candidates(store, "description")[0]
    original = EpisodeWorkspaceStore.update_state

    def displaced(self: EpisodeWorkspaceStore, *args: Any, **kwargs: Any) -> EpisodeWorkspace:
        self.layout.candidate_json_path("description", candidate.candidate_id).unlink()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EpisodeWorkspaceStore, "update_state", displaced)
    result = CliRunner().invoke(
        app,
        [
            "pick",
            "--workspace",
            str(tmp_path),
            "--asset-id",
            "description",
            "--candidate-id",
            str(candidate.candidate_id),
        ],
    )
    assert result.exit_code != 0, result.output
    assert "reload" in result.output.lower()
    assert not store.layout.state_json.exists()
    assert not store.layout.selected_text_path("description", candidate.format).exists()


@pytest.mark.parametrize("stop", ["abort", "interrupt", "failure"])
def test_pick_reports_each_commit_before_later_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    candidates = {asset: _candidates(store, asset) for asset in ("description", "shownotes")}
    output: list[str] = []
    original_echo = typer.echo

    def echo(message: Any = None, *args: Any, **kwargs: Any) -> None:
        output.append(str(message))
        original_echo(message, *args, **kwargs)

    calls = 0

    def prompt(*args: Any, **kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 1
        assert any(line.startswith("Selected description:") for line in output), output
        if stop == "abort":
            raise typer.Abort()
        if stop == "interrupt":
            raise KeyboardInterrupt()
        return 1

    original_save = pick.save_candidate_selection

    def save(**kwargs: Any) -> EpisodeWorkspace:
        if stop == "failure" and kwargs["candidate"].asset_id == "shownotes":
            raise OSError("Synthetic later failure\x1b]52;c;payload\x07")
        return original_save(**kwargs)

    monkeypatch.setattr(pick, "save_candidate_selection", save)
    monkeypatch.setattr(typer, "echo", echo)
    monkeypatch.setattr(typer, "prompt", prompt)
    result = CliRunner().invoke(app, ["pick", "--workspace", str(tmp_path)])
    assert result.exit_code != 0
    assert "Pick incomplete: 1/2" in result.output
    assert result.output.count("Selected description:") == 1
    assert "Selected shownotes:" not in result.output
    assert "\x1b" not in result.output
    state = store.read_state()
    assert state.assets[0].selected_candidate_id == candidates["description"][0].candidate_id
    assert not store.layout.selected_text_path("shownotes", candidates["shownotes"][0].format).exists()


def test_pick_success_receipts_name_exactly_published_choices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    candidates = _candidates(store, "description")
    monkeypatch.setattr(typer, "prompt", lambda *args, **kwargs: 2)
    result = CliRunner().invoke(app, ["pick", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert result.output.count("Selected description:") == 1
    assert str(candidates[1].candidate_id) in result.output
    assert "incomplete" not in result.output
    assert store.read_state().assets[0].selected_candidate_id == candidates[1].candidate_id
