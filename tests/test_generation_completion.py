from pathlib import Path
from typing import Any

import pytest

from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.entrypoints.draft_pipeline import _discover_chunk_ids
from podcast_pipeline.prompting import PromptRenderer, default_prompt_registry
from podcast_pipeline.summarization_llm import run_llm_summarization
from podcast_pipeline.summarization_stub import write_episode_summary_artifacts
from podcast_pipeline.transcript_chunker import ChunkerConfig, write_transcript_chunks
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore
from tests.test_summarization_llm import FakeDrafterRunner


def _store(tmp_path: Path) -> EpisodeWorkspaceStore:
    store = EpisodeWorkspaceStore(tmp_path)
    store.layout.transcript_dir.mkdir(parents=True)
    (store.layout.transcript_dir / "transcript.txt").write_text("one two three four five six")
    return store


def _chunk(store: EpisodeWorkspaceStore) -> None:
    write_transcript_chunks(
        layout=store.layout,
        transcript_path=store.layout.transcript_dir / "transcript.txt",
        config=ChunkerConfig(max_tokens=2, overlap_tokens=0),
    )


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}


@pytest.mark.parametrize("identity", [None, 1, 999])
def test_llm_request_identity_never_reaches_reduction(tmp_path: Path, identity: int | None) -> None:
    store = _store(tmp_path)
    _chunk(store)
    response: dict[str, Any] = {"summary_markdown": "synthetic chunk"}
    if identity is not None:
        response["chunk_id"] = identity
    runner = FakeDrafterRunner([response, {"summary_markdown": "synthetic episode"}])

    def run() -> None:
        run_llm_summarization(
            layout=store.layout, chunk_ids=[1], runner=runner, renderer=PromptRenderer(default_prompt_registry())
        )

    if identity == 999:
        with pytest.raises(ValueError, match="identity"):
            run()
        assert len(runner.prompts) == 1
        assert not store.layout.chunk_summary_json_path(1).exists()
        assert not store.layout.episode_summary_json_path().exists()
    else:
        run()
        import json

        assert json.loads(store.layout.chunk_summary_json_path(1).read_text())["chunk_id"] == 1


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        "chunk_0001.txt",
        "chunk_0001.json",
        "chunk_0002.txt",
        "chunk_0002.json",
        "chunk_0003.txt",
        "chunk_0003.json",
        "complete.json",
    ],
)
def test_chunk_publication_failure_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous: bool, failure: str
) -> None:
    store = _store(tmp_path)
    if previous:
        _chunk(store)
    before = _snapshot(store.layout.transcript_chunks_dir) if previous else {}
    original = Path.write_text
    injected: list[object] = []

    def fail(path: Path, text: str, *args: Any, **kwargs: Any) -> int:
        if path.name == failure:
            injected.append(path)
            raise OSError("synthetic disk failure")
        return original(path, text, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", fail)
        with pytest.raises(OSError):
            _chunk(store)
    assert injected
    assert _discover_chunk_ids(store) == ([1, 2, 3] if previous else [])
    if previous:
        assert _snapshot(store.layout.transcript_chunks_dir) == before
    _chunk(store)
    assert _discover_chunk_ids(store) == [1, 2, 3]


def test_chunk_set_rejects_legacy_missing_or_changed_files_and_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.layout.transcript_chunks_dir.mkdir()
    store.layout.transcript_chunk_text_path(1).write_text("legacy prefix")
    assert _discover_chunk_ids(store) == []
    _chunk(store)
    store.layout.transcript_chunk_meta_json_path(2).unlink()
    assert _discover_chunk_ids(store) == []
    _chunk(store)
    store.layout.transcript_chunk_text_path(1).write_text("corrupt")
    assert _discover_chunk_ids(store) == []
    _chunk(store)
    (store.layout.transcript_dir / "transcript.txt").write_text("other source")
    assert _discover_chunk_ids(store) == []


@pytest.mark.parametrize(
    "failure", ["render", "episode_summary.json", "episode_summary.md", "episode_summary.html", "complete.json"]
)
def test_summary_failure_preserves_complete_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    import podcast_pipeline.summarization_stub as module

    store = _store(tmp_path)
    _chunk(store)
    write_episode_summary_artifacts(layout=store.layout, episode_summary=EpisodeSummary(summary_markdown="old"))
    before = _snapshot(store.layout.episode_summary_dir)
    injected: list[object] = []
    original = module.atomic_write_text

    def fail(path: Path, text: str) -> None:
        if path.name == failure:
            injected.append(path)
            raise OSError("synthetic publication failure")
        original(path, text)

    def render(text: str) -> str:
        injected.append(text)
        raise OSError("synthetic render failure")

    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_write_text", fail)
        if failure == "render":
            patch.setattr(module, "markdown_to_deterministic_html", render)
        with pytest.raises(OSError):
            write_episode_summary_artifacts(
                layout=store.layout, episode_summary=EpisodeSummary(summary_markdown="new")
            )
    assert injected
    assert _snapshot(store.layout.episode_summary_dir) == before
    write_episode_summary_artifacts(layout=store.layout, episode_summary=EpisodeSummary(summary_markdown="new"))
    assert module.episode_summary_is_complete(store.layout)
    store.layout.episode_summary_html_path().unlink()
    assert not module.episode_summary_is_complete(store.layout)


def test_chunk_directory_replace_failure_restores_previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import podcast_pipeline.transcript_chunker as module

    store = _store(tmp_path)
    _chunk(store)
    before = _snapshot(store.layout.transcript_chunks_dir)
    replace = module.os.replace
    injected: list[object] = []

    def fail(source: Path, target: Path) -> None:
        if Path(source).name == "new":
            injected.append(source)
            raise OSError("synthetic rename failure")
        replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", fail)
        with pytest.raises(OSError):
            _chunk(store)
    assert injected
    assert _snapshot(store.layout.transcript_chunks_dir) == before
    assert _discover_chunk_ids(store) == [1, 2, 3]


def test_artifact_rollback_keeps_modes_and_failed_recovery_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stat

    import podcast_pipeline.workspace_store as module

    paths = [tmp_path / name for name in ["first", "second", "third"]]
    for path in paths:
        path.write_text("old")
    modes = {p: stat.S_IMODE(p.stat().st_mode) for p in paths}
    with pytest.raises(ValueError):
        with module.preserve_files(paths):
            for path in paths:
                path.write_text("new")
            raise ValueError("synthetic publication error")
    assert all(p.read_text() == "old" and stat.S_IMODE(p.stat().st_mode) == modes[p] for p in paths)
    replace = module.os.replace
    injected = []

    def fail(source: Path, target: Path) -> None:
        if target == paths[1]:
            injected.append(source)
            raise OSError("synthetic rollback error")
        replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", fail)
        with pytest.raises(module.WorkspaceStoreError, match="Artifact recovery failed"):
            with module.preserve_files(paths):
                for path in paths:
                    path.write_text("new")
                raise ValueError("synthetic publication error")
    assert injected
    assert paths[0].read_text() == paths[2].read_text() == "old"
    backups = list(tmp_path.glob(".second.backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == "old"


@pytest.mark.parametrize("interrupt", [False, True])
def test_unexpected_recovery_errors_keep_unconsumed_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    import podcast_pipeline.workspace_store as module

    paths = [tmp_path / name for name in ["first", "second"]]
    for path in paths:
        path.write_text("old")

    def fail(source: Path, target: Path) -> None:
        if interrupt:
            raise OSError("synthetic I/O recovery failure")
        raise ValueError("synthetic unexpected recovery failure")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", fail)
        with pytest.raises(KeyboardInterrupt if interrupt else ValueError):
            with module.preserve_files(paths):
                for path in paths:
                    path.write_text("new")
                raise KeyboardInterrupt("synthetic interrupt")
    for path in paths:
        backups = list(tmp_path.glob(f".{path.name}.backup-*"))
        assert len(backups) == 1 and backups[0].read_text() == "old"
