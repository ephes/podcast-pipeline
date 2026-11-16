from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
import yaml

from podcast_pipeline.entrypoints import draft_candidates, draft_demo
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def workspace(root: Path) -> EpisodeWorkspaceStore:
    store = EpisodeWorkspaceStore(root)
    store.write_episode_yaml({"episode_id": "synthetic"})
    path = store.layout.episode_summary_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"summary_markdown": "synthetic summary"}))
    return store


def generate(store: EpisodeWorkspaceStore, chapters: Path | None = None) -> None:
    draft_candidates.run_draft_candidates(workspace=store.layout.root, chapters=chapters, candidates_per_asset=1)


def assert_no_artifacts(store: EpisodeWorkspaceStore) -> None:
    assert not (store.layout.root / "copy").exists()
    assert not list(store.layout.root.rglob("protocol*.json"))


@pytest.mark.parametrize("kind", ["chapters", "summary", "lines"])
def test_excess_input_refused_before_publication(tmp_path: Path, kind: str) -> None:
    store = workspace(tmp_path / "workspace")
    chapters = tmp_path / "chapters.txt"
    chapters.write_text("chapter\n")
    if kind == "summary":
        store.layout.episode_summary_json_path().write_text(json.dumps({"summary_markdown": "x" * 1_048_576}))
    else:
        chapters.write_text("x" * 1_048_577 if kind == "chapters" else "chapter\n" * 201)
    with pytest.raises(typer.BadParameter):
        generate(store, chapters)
    assert_no_artifacts(store)


@pytest.mark.parametrize("key", ["chapters", "transcript"])
@pytest.mark.parametrize("reference", ["absolute", "traversal", "symlink"])
def test_metadata_references_stay_in_workspace(tmp_path: Path, key: str, reference: str) -> None:
    store = workspace(tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("SYNTHETIC_OUTSIDE_CANARY")
    (store.layout.root / "linked").symlink_to(outside, target_is_directory=True)
    refs = {"absolute": str(canary), "traversal": "../outside/canary.txt", "symlink": "linked/canary.txt"}
    store.layout.episode_yaml.write_text(yaml.safe_dump({"episode_id": "synthetic", "inputs": {key: refs[reference]}}))
    with pytest.raises(typer.BadParameter):
        draft_demo._read_input_text(store, key)
    if key == "chapters":
        with pytest.raises(typer.BadParameter):
            generate(store)
    assert_no_artifacts(store)


def test_fifo_draft_inputs_do_not_block(tmp_path: Path) -> None:
    store = workspace(tmp_path / "workspace")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    script = (
        "from pathlib import Path; import typer; "
        "from podcast_pipeline.entrypoints.draft_candidates import run_draft_candidates; "
        f"\ntry: run_draft_candidates(workspace=Path({str(store.layout.root)!r}),chapters=Path({str(fifo)!r}),"
        "candidates_per_asset=1)\nexcept typer.BadParameter: pass\nelse: raise AssertionError('admitted FIFO')"
    )
    subprocess.run([sys.executable, "-c", script], timeout=2, check=True)
    assert_no_artifacts(store)


def test_admitted_workspace_and_operator_inputs(tmp_path: Path) -> None:
    store = workspace(tmp_path / "workspace")
    chapters = tmp_path / "operator.txt"
    chapters.write_text("00:00 Synthetic chapter\n")
    generate(store, chapters)
    local = store.layout.root / "local.txt"
    local.write_text("00:00 Synthetic chapter\n")
    store.layout.episode_yaml.write_text(
        yaml.safe_dump({"episode_id": "synthetic", "inputs": {"chapters": "local.txt"}})
    )
    assert draft_candidates._load_chapters(store=store, chapters=None) == ["00:00 Synthetic chapter"]
    assert draft_demo._read_input_text(store, "chapters") == "00:00 Synthetic chapter\n"


@pytest.mark.parametrize("kind", ["summary", "chapters", "transcript"])
def test_changed_draft_source_refused_before_writes(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from podcast_pipeline import file_inputs

    store = workspace(tmp_path / "workspace")
    path = store.layout.episode_summary_json_path() if kind == "summary" else store.layout.root / f"{kind}.txt"
    if kind != "summary":
        path.write_text("synthetic input")
    store.layout.episode_yaml.write_text(yaml.safe_dump({"episode_id": "synthetic", "inputs": {kind: path.name}}))
    original_read = os.read

    def mutate(fd: int, size: int) -> bytes:
        value = original_read(fd, size)
        if value:
            path.write_text("changed generation")
        return value

    monkeypatch.setattr(file_inputs.os, "read", mutate)
    with pytest.raises(typer.BadParameter):
        if kind == "summary":
            generate(store)
        else:
            draft_demo._read_input_text(store, kind)
    assert_no_artifacts(store)
