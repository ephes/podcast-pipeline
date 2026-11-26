import logging
from pathlib import Path
from typing import Any

import pytest

from podcast_pipeline import transcript_chunker as chunker
from podcast_pipeline.entrypoints.draft_pipeline import _ingest_transcript
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore
from tests.test_chunk_publication_processes import _initial


@pytest.mark.parametrize("member", ["chunk_0001.txt", "chunk_0001.json", "complete.json"])
def test_short_stage_write_preserves_previous_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    layout = _initial(tmp_path, "fünf eins zwei drei vier sechs".encode())
    previous_config = chunker.ChunkerConfig(max_tokens=2, overlap_tokens=0)
    destination = layout.transcript_chunks_dir
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    write_text = Path.write_text

    def short_write(path: Path, content: str, *args: Any, **kwargs: Any) -> int:
        if path.parent.name == "new" and path.name == member:
            content = content[:-2]
        return write_text(path, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", short_write)
    with pytest.raises(OSError, match=rf"Short write staging .*{member}"):
        chunker.write_transcript_chunks(
            layout=layout,
            transcript_path=layout.transcript_dir / "transcript.txt",
            config=chunker.ChunkerConfig(max_tokens=4, overlap_tokens=0),
        )

    assert chunker.discover_complete_chunk_ids(layout, config=previous_config) == [1, 2, 3]
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before
    assert not list(destination.parent.glob(".chunks-*"))


def test_failed_restore_preserves_publication_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _initial(tmp_path)
    destination = layout.transcript_chunks_dir
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    replace = chunker.os.replace
    promotion_error = OSError("promotion refused")
    config = chunker.ChunkerConfig(max_tokens=4, overlap_tokens=0)

    def fail_promotion_and_restore(source: Path, target: Path) -> None:
        if target == destination:
            if source.name == "new":
                raise promotion_error
            if source.name.startswith(".chunks-previous-"):
                raise OSError("restore refused")
        replace(source, target)

    monkeypatch.setattr(chunker.os, "replace", fail_promotion_and_restore)
    with pytest.raises(OSError) as failure:
        chunker.write_transcript_chunks(
            layout=layout, transcript_path=layout.transcript_dir / "transcript.txt", config=config
        )

    assert failure.value is promotion_error
    assert not destination.exists()
    (backup,) = layout.transcript_dir.glob(".chunks-previous-*")
    assert {path.name: path.read_bytes() for path in backup.iterdir()} == before
    assert any("restore refused" in note and str(backup) in note for note in failure.value.__notes__)


@pytest.mark.parametrize("warning_fails", [False, True], ids=["warning-logged", "warning-handler-fails"])
def test_postcommit_retirement_failure_returns_new_generation_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, warning_fails: bool
) -> None:
    layout = _initial(tmp_path)
    before = {p.name: p.read_bytes() for p in layout.transcript_chunks_dir.iterdir()}
    rmtree = chunker.shutil.rmtree
    attempted_warnings: list[logging.LogRecord] = []

    class FailingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            attempted_warnings.append(record)
            raise RuntimeError("warning sink failed")

    def fail_backup(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path).name.startswith(".chunks-previous-"):
            raise OSError("private diagnostic sentinel")
        rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    config = chunker.ChunkerConfig(max_tokens=4, overlap_tokens=0)
    with monkeypatch.context() as patch:
        patch.setattr(chunker.shutil, "rmtree", fail_backup)
        if warning_fails:
            patch.setattr(logging.getLogger(chunker.__name__), "handlers", [FailingHandler()])
        metas = chunker.write_transcript_chunks(
            layout=layout, transcript_path=layout.transcript_dir / "transcript.txt", config=config
        )
    assert [meta.chunk_id for meta in metas] == [1, 2]
    assert [layout.root / meta.text_relpath for meta in metas] == [
        layout.transcript_chunk_text_path(chunk_id) for chunk_id in (1, 2)
    ]
    assert chunker.discover_complete_chunk_ids(layout, config=config) == [1, 2]
    backups = list(layout.transcript_dir.glob(".chunks-previous-*"))
    assert len(backups) == 1
    assert {p.name: p.read_bytes() for p in backups[0].iterdir()} == before
    (warning,) = attempted_warnings if warning_fails else caplog.records
    message = warning.getMessage()
    assert "committed" in message and str(backups[0]) in message
    assert "private diagnostic sentinel" not in message
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
