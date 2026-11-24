from dataclasses import replace
from pathlib import Path

import pytest

from podcast_pipeline import transcript_chunker as chunker
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


def test_expansion_limits_exact_utf8_boundaries() -> None:
    config = chunker.ChunkerConfig(max_tokens=2, overlap_tokens=1)
    # "é b \n" + "b c\n" = 10 UTF-8 bytes; trailing newlines count.
    limits = chunker.ChunkerLimits(source_bytes=6, tokens=3, chunks=2, output_bytes=10)
    assert len(chunker.chunk_transcript_text("é b c", config=config, limits=limits)) == 2
    for field in ("source_bytes", "tokens", "chunks", "output_bytes"):
        refused = replace(limits, **{field: getattr(limits, field) - 1})
        with pytest.raises(chunker.ChunkerResourceError, match=field):
            chunker.chunk_transcript_text("é b c", config=config, limits=refused)


def test_near_total_overlap_stops_before_retaining_excess_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    retained = 0
    original = chunker.TranscriptChunk

    def record(**kwargs: object) -> chunker.TranscriptChunk:
        nonlocal retained
        retained += 1
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(chunker, "TranscriptChunk", record)
    with pytest.raises(chunker.ChunkerResourceError, match="chunks"):
        chunker.chunk_transcript_text(
            "x " * 2000,
            config=chunker.ChunkerConfig(max_tokens=1000, overlap_tokens=999),
            limits=chunker.ChunkerLimits(chunks=3),
        )
    assert retained == 3


@pytest.mark.parametrize("field", ["source_bytes", "tokens", "chunks", "output_bytes"])
def test_refusal_preserves_published_set_and_discovery_is_bounded(tmp_path: Path, field: str) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    path = layout.transcript_dir / "transcript.txt"
    path.parent.mkdir()
    path.write_text("a b c")
    config = chunker.ChunkerConfig(max_tokens=2, overlap_tokens=1)
    chunker.write_transcript_chunks(layout=layout, transcript_path=path, config=config)
    before = {p.name: p.read_bytes() for p in layout.transcript_chunks_dir.iterdir()}
    limits = replace(chunker.ChunkerLimits(), **{field: 1})
    with pytest.raises(chunker.ChunkerResourceError):
        chunker.write_transcript_chunks(layout=layout, transcript_path=path, config=config, limits=limits)
    assert {p.name: p.read_bytes() for p in layout.transcript_chunks_dir.iterdir()} == before
    assert chunker.discover_complete_chunk_ids(layout, limits=limits) == []
    assert chunker.discover_complete_chunk_ids(layout) == [1, 2]


def test_discovery_and_recovery_charge_tokens_without_indexing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    source = layout.transcript_dir / "transcript.txt"
    source.parent.mkdir()
    source.write_text("é\u00a0two\n" * 2048, encoding="utf-8")
    config = chunker.ChunkerConfig(max_tokens=1024, overlap_tokens=0)
    limits = chunker.ChunkerLimits(tokens=4096)
    chunker.write_transcript_chunks(layout=layout, transcript_path=source, config=config, limits=limits)

    original = chunker._Token
    allocations = 0

    def record(start: int, end: int, byte_start: int) -> chunker._Token:
        nonlocal allocations
        allocations += 1
        return original(start, end, byte_start)

    monkeypatch.setattr(chunker, "_Token", record)
    refused = replace(limits, tokens=limits.tokens - 1)
    assert chunker.discover_complete_chunk_ids(layout, config=config, limits=refused) == []
    assert chunker.discover_complete_chunk_ids(layout, config=config, limits=limits) == [1, 2, 3, 4]

    backup = layout.transcript_dir / ".chunks-previous-budget"
    layout.transcript_chunks_dir.rename(backup)
    with pytest.raises(chunker.ChunkRecoveryError, match="Unusable chunk backup"):
        chunker.discover_complete_chunk_ids(layout, config=config, limits=refused)
    assert backup.is_dir()
    assert chunker.discover_complete_chunk_ids(layout, config=config, limits=limits) == [1, 2, 3, 4]
    assert not backup.exists()
    assert allocations == 0
