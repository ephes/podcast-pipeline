from pathlib import Path

import pytest

from podcast_pipeline import transcript_chunker as chunker
from podcast_pipeline.entrypoints.draft_pipeline import _ingest_transcript
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore
from tests.test_chunk_publication_processes import _initial


def test_postcommit_retirement_failure_returns_new_generation_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    layout = _initial(tmp_path)
    before = {p.name: p.read_bytes() for p in layout.transcript_chunks_dir.iterdir()}
    rmtree = chunker.shutil.rmtree

    def fail_backup(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path).name.startswith(".chunks-previous-"):
            raise OSError("private diagnostic sentinel")
        rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    config = chunker.ChunkerConfig(max_tokens=4, overlap_tokens=0)
    with monkeypatch.context() as patch:
        patch.setattr(chunker.shutil, "rmtree", fail_backup)
        metas = chunker.write_transcript_chunks(
            layout=layout, transcript_path=layout.transcript_dir / "transcript.txt", config=config
        )
    assert [meta.chunk_id for meta in metas] == [1, 2]
    assert chunker.discover_complete_chunk_ids(layout, config=config) == [1, 2]
    backups = list(layout.transcript_dir.glob(".chunks-previous-*"))
    assert len(backups) == 1
    assert {p.name: p.read_bytes() for p in backups[0].iterdir()} == before
    assert len(caplog.records) == 1
    assert "committed" in caplog.text and str(backups[0]) in caplog.text
    assert "private diagnostic sentinel" not in caplog.text
    # A later success retires only its own backup, never another generation's evidence.
    chunker.write_transcript_chunks(
        layout=layout, transcript_path=layout.transcript_dir / "transcript.txt", config=config
    )
    assert backups[0].exists()


def test_transcript_ingest_retires_retained_backup_so_new_source_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _initial(tmp_path)
    store = EpisodeWorkspaceStore(layout.root)
    store.write_episode_yaml({"episode_id": "test"})
    rmtree = chunker.shutil.rmtree

    def fail_backup(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path).name.startswith(".chunks-previous-"):
            raise OSError("cleanup refused")
        rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    config = chunker.ChunkerConfig(max_tokens=4, overlap_tokens=0)
    with monkeypatch.context() as patch:
        patch.setattr(chunker.shutil, "rmtree", fail_backup)
        chunker.write_transcript_chunks(
            layout=layout, transcript_path=layout.transcript_dir / "transcript.txt", config=config
        )
    assert len(list(layout.transcript_dir.glob(".chunks-previous-*"))) == 1

    source = tmp_path / "new_transcript.txt"
    source.write_text("alpha beta gamma delta epsilon")
    _ingest_transcript(store=store, transcript=source)

    assert not list(layout.transcript_dir.glob(".chunks-previous-*"))
    assert not layout.transcript_chunks_dir.exists()
    assert chunker.discover_complete_chunk_ids(layout, config=config) == []
    metas = chunker.write_transcript_chunks(
        layout=layout, transcript_path=layout.transcript_dir / "transcript.txt", config=config
    )
    assert [meta.chunk_id for meta in metas] == [1, 2]
    assert chunker.discover_complete_chunk_ids(layout, config=config) == [1, 2]
