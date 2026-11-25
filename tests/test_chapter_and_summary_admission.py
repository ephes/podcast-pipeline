import io
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from podcast_pipeline import asset_candidates_llm as assets
from podcast_pipeline import summarization_llm as summaries
from podcast_pipeline.domain.intermediate_formats import ChunkSummary, EpisodeSummary
from podcast_pipeline.domain.models import ProvenanceRef
from podcast_pipeline.prompting import PromptRenderer, default_prompt_registry, render_chunk_summary_prompt
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore


def chapter_workspace(root: Path, monkeypatch: pytest.MonkeyPatch, reference: str = "chapters.txt") -> Mock:
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(root / "absent.yaml"))
    store = EpisodeWorkspaceStore(root)
    store.write_episode_yaml({"episode_id": "synthetic", "inputs": {"chapters": reference}})
    summary = store.layout.episode_summary_json_path()
    summary.parent.mkdir(parents=True)
    summary.write_text(EpisodeSummary(summary_markdown="Synthetic summary").model_dump_json())
    chapter = root / reference
    chapter.parent.mkdir(parents=True, exist_ok=True)
    chapter.write_text("00:00 Intro\n")
    runner = Mock()
    runner.return_value.run.return_value = {"candidates": [{"content": "Synthetic candidate"}]}
    monkeypatch.setattr("podcast_pipeline.drafter_runner.DrafterCliRunner", runner)
    return runner


@pytest.mark.parametrize("kind", ["symlink", "fifo", "parent"])
def test_chapter_open_swap_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    root = tmp_path / "episode"
    reference = "nested/chapters.txt" if kind == "parent" else "chapters.txt"
    runner = chapter_workspace(root, monkeypatch, reference)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "chapters.txt").write_text("PRIVATE_SENTINEL")
    chapter = root / reference
    original_open = os.open
    original_io_open = io.open
    swapped = False

    def swap() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        if kind == "parent":
            (root / "nested").rename(root / "saved")
            (root / "nested").symlink_to(outside, target_is_directory=True)
        else:
            chapter.unlink()
            if kind == "fifo":
                os.mkfifo(chapter)
            else:
                chapter.symlink_to(outside / "chapters.txt")

    def opening(path: Any, *args: Any, **kwargs: Any) -> int:
        if str(path).endswith("chapters.txt") or (kind == "parent" and str(path) == "nested"):
            swap()
        return original_open(path, *args, **kwargs)

    def io_opening(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path).endswith("chapters.txt"):
            if kind == "fifo":
                # Avoid hanging the vulnerable implementation; a FIFO path reopen is already a failure.
                raise AssertionError("Blocking chapter path reopen")
            swap()
        return original_io_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(io, "open", io_opening)
    with pytest.raises(RuntimeError, match="chapters input") as error:
        assets.generate_single_asset_candidates_llm(workspace=root, asset_id="description", candidates_per_asset=1)
    assert "PRIVATE_SENTINEL" not in str(error.value)
    runner.assert_not_called()


def test_chapter_changes_during_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = chapter_workspace(tmp_path, monkeypatch)
    chapters = tmp_path / "chapters.txt"
    chapter_inode = chapters.stat().st_ino
    original = os.read
    changed = False

    def read(fd: int, size: int) -> bytes:
        nonlocal changed
        data = original(fd, size)
        if data and not changed and os.fstat(fd).st_ino == chapter_inode:
            changed = True
            chapters.write_text("00:00 Changed\n")
        return data

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(RuntimeError, match="chapters input"):
        assets.generate_single_asset_candidates_llm(workspace=tmp_path, asset_id="description", candidates_per_asset=1)
    runner.assert_not_called()


@pytest.mark.parametrize("replace_parent", [False, True])
def test_chapter_named_authority_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace_parent: bool
) -> None:
    root = tmp_path / "episode"
    runner = chapter_workspace(root, monkeypatch, "nested/chapters.txt")
    chapter_inode = (root / "nested/chapters.txt").stat().st_ino
    original = os.read
    changed = False

    def read(fd: int, size: int) -> bytes:
        nonlocal changed
        data = original(fd, size)
        if data and not changed and os.fstat(fd).st_ino == chapter_inode:
            changed = True
            if replace_parent:
                (root / "nested").rename(root / "saved")
                (root / "nested").mkdir()
                (root / "nested/chapters.txt").write_text("00:00 Different\n")
            else:
                (root / "nested/chapters.txt").unlink()
                (root / "nested/chapters.txt").write_text("00:00 Different\n")
        return data

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(RuntimeError, match="chapters input"):
        assets.generate_single_asset_candidates_llm(workspace=root, asset_id="description", candidates_per_asset=1)
    runner.assert_not_called()


@pytest.mark.parametrize("kind", ["bytes", "lines"])
def test_chapter_envelope_before_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    runner = chapter_workspace(tmp_path, monkeypatch)
    if kind == "bytes":
        monkeypatch.setattr(assets, "MAX_CHAPTER_BYTES", 10, raising=False)
    else:
        monkeypatch.setattr(assets, "MAX_CHAPTER_LINES", 1, raising=False)
        (tmp_path / "chapters.txt").write_text("00:00 Intro\n00:10 Main\n")
    with pytest.raises(RuntimeError, match="chapters input"):
        assets.generate_single_asset_candidates_llm(workspace=tmp_path, asset_id="description", candidates_per_asset=1)
    runner.assert_not_called()


@pytest.mark.parametrize("kind", ["source", "prompt", "hosts"])
def test_chunk_byte_admission_preserves_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    chunk = layout.transcript_chunk_text_path(1)
    chunk.parent.mkdir(parents=True)
    chunk.write_text("ü" * 100, encoding="utf-8")
    cache = layout.chunk_summary_json_path(1)
    cache.parent.mkdir(parents=True)
    cache.write_text("previous cache")
    monkeypatch.setattr(summaries, "MAX_CHUNK_SOURCE_BYTES", 199 if kind == "source" else 200, raising=False)
    monkeypatch.setattr(summaries, "MAX_SUMMARIZER_PROMPT_BYTES", 256 if kind != "hosts" else 2048, raising=False)
    runner = Mock()
    runner.run.return_value = {"summary_markdown": "replacement"}
    with pytest.raises(RuntimeError, match="[Bb]yte|prompt|source"):
        summaries.summarize_transcript_chunks_llm(
            layout=layout,
            chunk_ids=[1],
            runner=runner,
            renderer=PromptRenderer(default_prompt_registry()),
            hosts=["ü" * 2049] if kind == "hosts" else None,
        )
    runner.run.assert_not_called()
    assert cache.read_text() == "previous cache"


def test_complete_chunk_prompt_exact_byte_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    chunk = layout.transcript_chunk_text_path(1)
    chunk.parent.mkdir(parents=True)
    chunk.write_text("Grüße", encoding="utf-8")
    renderer = PromptRenderer(default_prompt_registry())
    prompt = render_chunk_summary_prompt(renderer=renderer, chunk_id=1, chunk_text="Grüße", hosts=["Synthetic Host"])
    size = len(prompt.text.encode())
    runner = Mock()
    runner.run.return_value = {"summary_markdown": "Summary"}
    monkeypatch.setattr(summaries, "MAX_SUMMARIZER_PROMPT_BYTES", size, raising=False)
    result = summaries.summarize_transcript_chunks_llm(
        layout=layout, chunk_ids=[1], runner=runner, renderer=renderer, hosts=["Synthetic Host"]
    )
    assert runner.run.call_args.args == (prompt.text,)
    assert result[0].provenance[0].ref == "chunk_v1"
    runner.reset_mock()
    monkeypatch.setattr(summaries, "MAX_SUMMARIZER_PROMPT_BYTES", size - 1)
    with pytest.raises(RuntimeError):
        summaries.summarize_transcript_chunks_llm(
            layout=layout, chunk_ids=[1], runner=runner, renderer=renderer, hosts=["Synthetic Host"]
        )
    runner.run.assert_not_called()


def test_reduction_aggregate_and_complete_prompt_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [
        ChunkSummary(
            chunk_id=i, summary_markdown="ü" * 100, provenance=[ProvenanceRef(kind="synthetic", ref=f"source-{i}")]
        )
        for i in range(1, 21)
    ]
    renderer = PromptRenderer(default_prompt_registry())
    runner = Mock()
    runner.run.return_value = {"summary_markdown": "Episode"}
    monkeypatch.setattr(summaries, "MAX_SUMMARIZER_PROMPT_BYTES", 2048, raising=False)
    with pytest.raises(RuntimeError):
        summaries.reduce_chunk_summaries_to_episode_summary_llm(
            chunk_summaries=chunks, runner=runner, renderer=renderer
        )
    runner.run.assert_not_called()
    monkeypatch.setattr(summaries, "MAX_SUMMARIZER_PROMPT_BYTES", 32768)
    result = summaries.reduce_chunk_summaries_to_episode_summary_llm(
        chunk_summaries=chunks, runner=runner, renderer=renderer
    )
    prompt = runner.run.call_args.args[0]
    assert "ü" * 100 in prompt
    rendered_json = prompt.split("Chunk summaries (JSON array):\n", 1)[1].split("\n\n", 1)[0]
    assert json.loads(rendered_json) == [chunk.model_dump(mode="json") for chunk in chunks]
    assert result.provenance[0].ref == "episode_v1"
    size = len(prompt.encode())
    runner.reset_mock()
    monkeypatch.setattr(summaries, "MAX_SUMMARIZER_PROMPT_BYTES", size)
    summaries.reduce_chunk_summaries_to_episode_summary_llm(chunk_summaries=chunks, runner=runner, renderer=renderer)
    assert runner.run.call_args.args == (prompt,)
    runner.reset_mock()
    monkeypatch.setattr(summaries, "MAX_SUMMARIZER_PROMPT_BYTES", size - 1)
    with pytest.raises(RuntimeError):
        summaries.reduce_chunk_summaries_to_episode_summary_llm(
            chunk_summaries=chunks, runner=runner, renderer=renderer
        )
    runner.run.assert_not_called()
