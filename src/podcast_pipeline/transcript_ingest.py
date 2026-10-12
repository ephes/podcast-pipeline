from __future__ import annotations

import filecmp
import os
import shutil
import tempfile
from pathlib import Path

import typer

from podcast_pipeline.workspace_store import EpisodeWorkspaceStore, atomic_write_text


def _clear_stale_artifacts(store: EpisodeWorkspaceStore) -> None:
    """Remove existing chunks and summaries so they are rebuilt from a new transcript."""
    chunks_dir = store.layout.transcript_chunks_dir
    if chunks_dir.exists():
        shutil.rmtree(chunks_dir)
        typer.echo("Cleared stale chunks", err=True)

    summaries_dir = store.layout.summaries_dir
    if summaries_dir.exists():
        shutil.rmtree(summaries_dir)
        typer.echo("Cleared stale summaries", err=True)


def prepare_transcript(*, store: EpisodeWorkspaceStore, transcript: Path | None = None) -> None:
    """Preserve current artifacts while ingesting, or finish a pending ingest."""
    if transcript is None:
        _finish_transcript_ingest(store)
        return
    transcript_dir = store.layout.transcript_dir
    transcript_dest = transcript_dir / "transcript.txt"
    # Opening first rejects missing/unreadable sources before touching artifacts.
    with transcript.open("rb"):
        if transcript_dest.exists() and transcript.samefile(transcript_dest):
            _finish_transcript_ingest(store)
            return
    store.read_episode_yaml()
    transcript_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=transcript_dir, prefix=".transcript-", delete=False) as temporary:
        staged = Path(temporary.name)
    try:
        shutil.copyfile(transcript, staged)
        if transcript_dest.exists() and filecmp.cmp(staged, transcript_dest, shallow=False):
            _finish_transcript_ingest(store)
            return
        if transcript_dest.exists():
            shutil.copymode(transcript_dest, staged)
        with staged.open("rb") as copied:
            os.fsync(copied.fileno())
        pending = transcript_dir / ".ingest-pending"
        already_pending = pending.exists()
        try:
            atomic_write_text(pending, "pending\n")
            os.replace(staged, transcript_dest)
        except OSError:
            if not already_pending:
                pending.unlink(missing_ok=True)
            raise
        directory_fd = os.open(transcript_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        staged.unlink(missing_ok=True)
    _finish_transcript_ingest(store)


def _finish_transcript_ingest(store: EpisodeWorkspaceStore) -> None:
    """Resume derived-artifact invalidation after a published transcript."""
    pending = store.layout.transcript_dir / ".ingest-pending"
    if not pending.exists():
        return
    _clear_stale_artifacts(store)
    transcript_dest = store.layout.transcript_dir / "transcript.txt"
    with store.update_episode_yaml() as episode_yaml:
        inputs = episode_yaml.get("inputs")
        if not isinstance(inputs, dict):
            inputs = {}
        inputs = dict(inputs)
        inputs["transcript"] = str(transcript_dest.relative_to(store.layout.root))
        episode_yaml["inputs"] = inputs
    pending.unlink()
