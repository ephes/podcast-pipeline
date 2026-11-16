from pathlib import Path

import pytest

from podcast_pipeline import transcript_chunker as chunker
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
