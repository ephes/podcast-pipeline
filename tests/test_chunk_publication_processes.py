"""Disposable publishers/readers coordinated at directory rename boundaries."""

import json
import multiprocessing as mp
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from podcast_pipeline import transcript_chunker as chunker
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


def _publish(root: str, connection: Connection, max_tokens: int, pause_gap: bool, fail: bool) -> None:
    layout = EpisodeWorkspaceLayout(root=Path(root))
    replace = chunker.os.replace
    write = Path.write_text

    def barrier_write(path: Path, text: str, *args: Any, **kwargs: Any) -> int:
        result = write(path, text, *args, **kwargs)
        if path.name == "complete.json":
            connection.send("staged")
            connection.recv()
        return result

    def barrier_replace(source: Path, destination: Path) -> None:
        if Path(source).name != "new":
            replace(source, destination)
        if Path(source) == layout.transcript_chunks_dir and pause_gap:
            connection.send("gap")
            connection.recv()
        if Path(source).name == "new":
            if fail:
                raise OSError("injected swap failure")
            replace(source, destination)

    patch = pytest.MonkeyPatch()
    patch.setattr(Path, "write_text", barrier_write)
    patch.setattr(chunker.os, "replace", barrier_replace)
    try:
        chunker.write_transcript_chunks(
            layout=layout,
            transcript_path=layout.transcript_dir / "transcript.txt",
            config=chunker.ChunkerConfig(max_tokens=max_tokens, overlap_tokens=0),
        )
        connection.send("committed")
    except Exception as exc:
        connection.send(type(exc).__name__)


def _discover(root: str, connection: Connection) -> None:
    connection.send("reading")
    connection.send(chunker.discover_complete_chunk_ids(EpisodeWorkspaceLayout(root=Path(root))))


def _receive(connection: Connection) -> Any:
    assert connection.poll(10), "child did not reach its barrier"
    return connection.recv()


def _initial(root: Path) -> EpisodeWorkspaceLayout:
    layout = EpisodeWorkspaceLayout(root=root)
    path = layout.transcript_dir / "transcript.txt"
    path.parent.mkdir(parents=True)
    path.write_text("one two three four five six")
    chunker.write_transcript_chunks(
        layout=layout, transcript_path=path, config=chunker.ChunkerConfig(max_tokens=2, overlap_tokens=0)
    )
    return layout


def test_source_changed_during_staging_cannot_replace_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _initial(tmp_path)
    before = {p.name: p.read_bytes() for p in layout.transcript_chunks_dir.iterdir()}
    write = Path.write_text
    source = layout.transcript_dir / "transcript.txt"

    def change(path: Path, text: str, *args: Any, **kwargs: Any) -> int:
        result = write(path, text, *args, **kwargs)
        if path.name == "complete.json":
            write(source, "changed after staging")
        return result

    monkeypatch.setattr(Path, "write_text", change)
    with pytest.raises(ValueError, match="Transcript changed"):
        chunker.write_transcript_chunks(layout=layout, transcript_path=source, config=chunker.ChunkerConfig())
    assert {p.name: p.read_bytes() for p in layout.transcript_chunks_dir.iterdir()} == before


@pytest.mark.parametrize("failed_swap", [False, True])
def test_two_writers_and_reader_share_publication_boundary(tmp_path: Path, failed_swap: bool) -> None:
    layout = _initial(tmp_path)
    ctx = mp.get_context("spawn")
    first, first_child = ctx.Pipe()
    second, second_child = ctx.Pipe()
    reader, reader_child = ctx.Pipe()
    a = ctx.Process(target=_publish, args=(str(tmp_path), first_child, 4, True, failed_swap))
    b = ctx.Process(target=_publish, args=(str(tmp_path), second_child, 3, False, False))
    r = ctx.Process(target=_discover, args=(str(tmp_path), reader_child))
    processes = [a, b, r]
    try:
        a.start()
        assert _receive(first) == "staged"
        first.send("continue")
        assert _receive(first) == "gap"
        r.start()
        assert _receive(reader) == "reading"
        b.start()
        assert _receive(second) == "staged"  # expensive staging stays outside the lock
        second.send("continue")
        assert not reader.poll(0.3), "reader observed the inter-rename gap"
        assert not second.poll(0.3), "second writer entered another publisher's swap"
        first.send("continue")
        assert _receive(first) == ("OSError" if failed_swap else "committed")
        assert _receive(second) == "committed"
        assert _receive(reader) in ([1, 2, 3], [1, 2])
        assert chunker.discover_complete_chunk_ids(layout) == [1, 2]
        marker = json.loads((layout.transcript_chunks_dir / "complete.json").read_text())
        assert marker["config"]["max_tokens"] == 3
        assert not list(layout.transcript_dir.glob(".chunks-previous-*"))
    finally:
        for process in processes:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(10)
