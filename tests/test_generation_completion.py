import os
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


@pytest.mark.parametrize("grows_during_read", [False, True])
def test_summary_verification_caps_output_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grows_during_read: bool
) -> None:
    import podcast_pipeline.summarization_stub as module

    store = _store(tmp_path)
    _chunk(store)
    write_episode_summary_artifacts(layout=store.layout, episode_summary=EpisodeSummary(summary_markdown="x" * 1024))
    assert module.episode_summary_is_complete(store.layout)  # Existing version-1 receipt.
    target = store.layout.episode_summary_markdown_path()
    original_bytes = target.read_bytes()
    max_bytes = 128
    monkeypatch.setattr(module, "_SUMMARY_MARKDOWN_BYTES", max_bytes, raising=False)
    if grows_during_read:
        target.write_bytes(original_bytes[:16])
    identity = target.stat()
    total_read = 0

    def append_remainder() -> None:
        with target.open("ab") as output:
            output.write(original_bytes[16:])

    read = os.read
    read_bytes = Path.read_bytes

    def observed_read(fd: int, size: int) -> bytes:
        nonlocal total_read
        if not os.path.samestat(os.fstat(fd), identity):
            return read(fd, size)
        if grows_during_read and total_read == 0:
            append_remainder()
        data = read(fd, size)
        total_read += len(data)
        return data

    def observed_read_bytes(path: Path) -> bytes:
        nonlocal total_read
        if path == target and grows_during_read:
            append_remainder()
        data = read_bytes(path)
        if path == target:
            total_read += len(data)
        return data

    monkeypatch.setattr(os, "read", observed_read)
    monkeypatch.setattr(Path, "read_bytes", observed_read_bytes)
    complete = module.episode_summary_is_complete(store.layout)
    assert total_read == (max_bytes + 1 if grows_during_read else 0)
    assert not complete


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
    backups = list(tmp_path.glob(".backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == "old"


@pytest.mark.parametrize("interrupt", [False, True])
def test_unexpected_recovery_errors_keep_unconsumed_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    import podcast_pipeline.workspace_store as module

    paths = [tmp_path / name for name in ["first", "second"]]
    for path in paths:
        path.write_text(path.name)

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
    backups = list(tmp_path.glob(".backup-*"))
    assert sorted(backup.read_text() for backup in backups) == ["first", "second"]


@pytest.mark.parametrize(
    ("limit_module", "limit_name", "limit", "artifact", "oversized"),
    [
        ("summary_inputs", "MAX_SUMMARY_BYTES", 256, "episode_summary.json", "é" * 60),
        ("summarization_stub", "_SUMMARY_MARKDOWN_BYTES", 8, "episode_summary.md", "é" * 4),
        ("summarization_stub", "_SUMMARY_HTML_BYTES", 80, "episode_summary.html", "&" * 8),
    ],
)
def test_oversized_summary_preserves_complete_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_module: str,
    limit_name: str,
    limit: int,
    artifact: str,
    oversized: str,
) -> None:
    from podcast_pipeline import summarization_stub, summary_inputs

    module = summary_inputs if limit_module == "summary_inputs" else summarization_stub
    monkeypatch.setattr(module, limit_name, limit)
    store = _store(tmp_path)
    _chunk(store)
    old_summary = EpisodeSummary(summary_markdown="old")
    write_episode_summary_artifacts(layout=store.layout, episode_summary=old_summary)
    before = _snapshot(store.layout.episode_summary_dir)
    assert summarization_stub.load_complete_episode_summary(store.layout) == old_summary
    assert summary_inputs.load_workspace_episode_summary(store.layout) == old_summary

    with pytest.raises(ValueError, match=artifact):
        write_episode_summary_artifacts(
            layout=store.layout, episode_summary=EpisodeSummary(summary_markdown=oversized)
        )

    assert _snapshot(store.layout.episode_summary_dir) == before
    assert summarization_stub.load_complete_episode_summary(store.layout) == old_summary
