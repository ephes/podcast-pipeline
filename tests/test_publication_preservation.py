from __future__ import annotations

import json
import os
from pathlib import Path
from typing import IO, Any, cast

import pytest

from podcast_pipeline import transcript_ingest, workspace_store
from podcast_pipeline.agent_runners import FakeCreatorRunner, FakeReviewerRunner
from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace, TextFormat
from podcast_pipeline.entrypoints import draft_pipeline
from podcast_pipeline.entrypoints.pick_web import _ServerContext
from podcast_pipeline.review_loop_engine import ProtocolWrite
from podcast_pipeline.review_loop_orchestrator import _write_protocol_files, run_review_loop_orchestrator
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def test_protocol_serialization_precedes_all_mutation(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    first.write_text("previous\n")
    second = tmp_path / "missing" / "second.json"
    with pytest.raises(TypeError):
        _write_protocol_files((ProtocolWrite(first, {"ok": True}), ProtocolWrite(second, {"bad": object()})))
    assert first.read_text() == "previous\n"
    assert not second.parent.exists()


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_protocol_failed_publication_preserves_file_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep_001"})
    store.write_state(EpisodeWorkspace(episode_id="ep_001", root_dir="."))
    protocol = store.layout.protocol_iteration_json_path("description", 1)
    _write_protocol_files((ProtocolWrite(protocol, {"previous": True}),))
    before_protocol = protocol.read_bytes()
    before_state = store.layout.state_json.read_bytes()
    called = False
    if failure == "replace":
        original_replace = os.replace

        def fail_replace(source: str | Path, destination: str | Path) -> None:
            nonlocal called
            if Path(destination) == protocol:
                called = True
                raise OSError("injected replace failure")
            original_replace(source, destination)

        monkeypatch.setattr(workspace_store.os, "replace", fail_replace)
    else:

        def fail_fsync(fd: int) -> None:
            nonlocal called
            called = True
            raise OSError("injected flush failure")

        monkeypatch.setattr(workspace_store.os, "fsync", fail_fsync)
    creator = FakeCreatorRunner(layout=store.layout, replies=[{"done": True, "candidate": {"content": "new"}}])
    reviewer = FakeReviewerRunner(layout=store.layout, reviewer="reviewer_a", replies=[{"verdict": "ok"}])
    with pytest.raises(OSError):
        run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=1, creator=creator, reviewer=reviewer
        )
    assert called
    assert protocol.read_bytes() == before_protocol
    assert store.layout.state_json.read_bytes() == before_state
    assert not list(protocol.parent.glob("*.tmp"))


def test_protocol_success_documents_parse(tmp_path: Path) -> None:
    writes = tuple(ProtocolWrite(tmp_path / f"{i}.json", {"iteration": i}) for i in range(3))
    _write_protocol_files(writes)
    assert [json.loads(write.path.read_text()) for write in writes] == [write.json_data for write in writes]


def test_selection_switch_removes_superseded_formats(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_selected_text("description", TextFormat.markdown, "# A")
    store.write_selected_text("description", TextFormat.plain, "B")
    assert store.read_selected_text("description", TextFormat.plain) == "B\n"
    for fmt in (TextFormat.markdown, TextFormat.html):
        assert not store.layout.selected_text_path("description", fmt).exists()
    store.write_selected_text("description", TextFormat.markdown, "# C")
    assert store.read_selected_text("description", TextFormat.markdown) == "# C\n"
    assert "C" in store.read_selected_text("description", TextFormat.html)
    assert not store.layout.selected_text_path("description", TextFormat.plain).exists()
    store.write_selected_text("description", TextFormat.html, "<p>D</p>")
    assert not store.layout.selected_text_path("description", TextFormat.markdown).exists()
    assert store.read_selected_text("description", TextFormat.html) == "<p>D</p>\n"


@pytest.mark.parametrize("fmt", [TextFormat.plain, TextFormat.markdown])
@pytest.mark.parametrize("failure", ["stage", "replace"])
def test_selection_failure_preserves_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: TextFormat, failure: str
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_selected_text("description", TextFormat.markdown, "# A")
    before = {f: store.read_selected_text("description", f) for f in (TextFormat.markdown, TextFormat.html)}
    target = store.layout.selected_text_path("description", TextFormat.html if fmt == TextFormat.markdown else fmt)
    called = False
    original_replace = os.replace

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        nonlocal called
        destination = Path(destination)
        at_boundary = destination == target if failure == "replace" else destination.name == target.name
        if at_boundary and not called:
            called = True
            raise OSError("injected selection failure")
        original_replace(source, destination)

    monkeypatch.setattr(workspace_store.os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.write_selected_text("description", fmt, "B")
    assert called
    for old_format, old_text in before.items():
        assert store.read_selected_text("description", old_format) == old_text
    assert not store.layout.selected_text_path("description", TextFormat.plain).exists()
    assert not list(target.parent.glob(".selection-*"))


def _transcript_workspace(tmp_path: Path) -> tuple[EpisodeWorkspaceStore, Path, dict[Path, bytes]]:
    store = EpisodeWorkspaceStore(tmp_path / "workspace")
    store.write_episode_yaml({"episode_id": "ep_001", "inputs": {"transcript": "transcript/transcript.txt"}})
    destination = store.layout.transcript_dir / "transcript.txt"
    chunk = store.layout.transcript_chunks_dir / "chunk_0001.txt"
    summary = store.layout.summaries_dir / "summary.json"
    for path, content in ((destination, b"old transcript\n"), (chunk, b"old chunk\n"), (summary, b"{}\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    paths = (destination, chunk, summary, store.layout.episode_yaml)
    return store, destination, {path: path.read_bytes() for path in paths}


@pytest.mark.parametrize("source_kind", ["missing", "self", "hardlink", "unchanged"])
def test_transcript_missing_or_unchanged_ingest_preserves_current(tmp_path: Path, source_kind: str) -> None:
    store, destination, before = _transcript_workspace(tmp_path)
    source = tmp_path / "source.txt"
    if source_kind == "self":
        source = destination
    elif source_kind == "hardlink":
        source.hardlink_to(destination)
    elif source_kind == "unchanged":
        source.write_bytes(destination.read_bytes())
    if source_kind == "missing":
        with pytest.raises(OSError):
            transcript_ingest.prepare_transcript(store=store, transcript=source)
    else:
        transcript_ingest.prepare_transcript(store=store, transcript=source)
    assert {path: path.read_bytes() for path in before} == before
    assert not list(destination.parent.glob(".transcript-*"))


@pytest.mark.parametrize("source_kind", ["unreadable", "copy", "replace"])
def test_transcript_io_failure_preserves_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    store, destination, before = _transcript_workspace(tmp_path)
    source = tmp_path / "source.txt"
    source.write_bytes(b"new transcript\n")
    called = False
    if source_kind == "copy":

        def fail_copy(src: Path, dst: Path) -> None:
            nonlocal called
            called = True
            dst.write_bytes(b"partial")
            raise OSError("injected copy failure")

        monkeypatch.setattr(transcript_ingest.shutil, "copyfile", fail_copy)
    elif source_kind == "replace":
        original_replace = os.replace

        def fail_replace(src: Path, dst: Path) -> None:
            nonlocal called
            if dst == destination:
                called = True
                raise OSError("injected replace failure")
            original_replace(src, dst)

        monkeypatch.setattr(transcript_ingest.os, "replace", fail_replace)
    elif source_kind == "unreadable":
        original_open = Path.open

        def fail_open(path: Path, *args: Any, **kwargs: Any) -> IO[Any]:
            nonlocal called
            if path == source:
                called = True
                raise PermissionError("injected unreadable source")
            return cast(IO[Any], original_open(path, *args, **kwargs))

        monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(OSError):
        transcript_ingest.prepare_transcript(store=store, transcript=source)
    assert called
    assert {path: path.read_bytes() for path in before} == before
    assert not list(destination.parent.glob(".transcript-*"))


def test_transcript_success_invalidates_only_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, destination, _ = _transcript_workspace(tmp_path)
    source = tmp_path / "source.txt"
    source.write_bytes(b"new transcript\n")
    original_clear = transcript_ingest._clear_stale_artifacts
    called = False

    def verify_clear(store: EpisodeWorkspaceStore) -> None:
        nonlocal called
        called = True
        assert destination.read_bytes() == b"new transcript\n"
        original_clear(store)

    monkeypatch.setattr(transcript_ingest, "_clear_stale_artifacts", verify_clear)
    transcript_ingest.prepare_transcript(store=store, transcript=source)
    assert called
    assert not store.layout.transcript_chunks_dir.exists()
    assert not store.layout.summaries_dir.exists()
    assert store.read_episode_yaml()["inputs"]["transcript"] == str(destination.relative_to(store.layout.root))


def test_pick_format_switch_keeps_selection_metadata_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep_001"})
    old = Candidate(asset_id="description", content="# A")
    new = Candidate(asset_id="description", format=TextFormat.plain, content="B")
    ctx = _ServerContext(
        store=store,
        candidates_by_asset={"description": [old, new]},
        workspace_state=EpisodeWorkspace(episode_id="ep_001", root_dir="."),
    )
    assert ctx.select_candidate("description", str(old.candidate_id)) is None
    state_before = store.layout.state_json.read_bytes()
    original_replace = os.replace
    called = False

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        nonlocal called
        if Path(destination) == store.layout.selected_text_path("description", TextFormat.plain):
            called = True
            raise OSError("injected pick failure")
        original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(workspace_store.os, "replace", fail_replace)
        with pytest.raises(OSError):
            ctx.select_candidate("description", str(new.candidate_id))
    assert called
    assert store.layout.state_json.read_bytes() == state_before
    assert ctx.workspace_state.assets[0].selected_candidate_id == old.candidate_id
    assert store.read_selected_text("description", TextFormat.markdown) == "# A\n"
    assert ctx.select_candidate("description", str(new.candidate_id)) is None
    assert store.read_state().assets[0].selected_candidate_id == new.candidate_id
    assert ctx.workspace_state.assets[0].selected_candidate_id == new.candidate_id
    assert store.read_selected_text("description", TextFormat.plain) == "B\n"
    assert not store.layout.selected_text_path("description", TextFormat.html).exists()


@pytest.mark.parametrize("operation", ["candidate", "curated"])
def test_dashboard_tag_failure_preserves_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from podcast_pipeline.dashboard_context import DashboardContext

    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep_001"})
    candidate = Candidate(asset_id="cms_tags", content="python, testing")
    store.write_candidate(candidate)
    ctx = DashboardContext(workspace=tmp_path)
    assert ctx.select_candidate("cms_tags", str(candidate.candidate_id)) is None
    assert ctx.set_selected_tags("cms_tags", ["hand-curated", "keep-me"]) is None
    paths = [store.layout.selected_text_path("cms_tags", fmt) for fmt in (TextFormat.markdown, TextFormat.html)]
    paths.append(store.layout.state_json)
    before = {path: path.read_bytes() for path in paths}
    called = False
    original_replace = os.replace

    def fail_replace(source: str | Path, target: str | Path) -> None:
        nonlocal called
        if Path(target) == paths[1]:
            called = True
            raise OSError("injected dashboard publication failure")
        original_replace(source, target)

    monkeypatch.setattr(workspace_store.os, "replace", fail_replace)
    with pytest.raises(OSError):
        if operation == "candidate":
            ctx.select_candidate("cms_tags", str(candidate.candidate_id))
        else:
            ctx.set_selected_tags("cms_tags", ["replacement"])
    assert called
    assert {path: path.read_bytes() for path in paths} == before
    assert ctx.get_selected_tags("cms_tags") == ["hand-curated", "keep-me"]


@pytest.mark.parametrize("failure", ["invalidation", "metadata"])
@pytest.mark.parametrize("retry", ["self", "identical", "reuse"])
def test_transcript_retry_finishes_pending_before_drafting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, retry: str
) -> None:
    from podcast_pipeline.summarization_stub import StubSummarizerConfig
    from podcast_pipeline.transcript_chunker import ChunkerConfig

    store, destination, _ = _transcript_workspace(tmp_path)
    source = tmp_path / "source.txt"
    source.write_bytes(b"new transcript\n")
    pending = destination.parent / ".ingest-pending"
    original_replace = os.replace
    called = False

    def fail_clear(store: EpisodeWorkspaceStore) -> None:
        nonlocal called
        called = True
        raise OSError("injected invalidation failure")

    def fail_metadata(src: str | Path, dst: str | Path) -> None:
        nonlocal called
        if Path(dst) == store.layout.episode_yaml:
            called = True
            raise OSError("injected metadata failure")
        original_replace(src, dst)

    with monkeypatch.context() as patch:
        if failure == "invalidation":
            patch.setattr(transcript_ingest, "_clear_stale_artifacts", fail_clear)
        else:
            patch.setattr(workspace_store.os, "replace", fail_metadata)
        with pytest.raises(OSError):
            transcript_ingest.prepare_transcript(store=store, transcript=source)
    assert called
    assert destination.read_bytes() == b"new transcript\n"
    assert pending.exists()
    drafted = False

    def verify_draft(**kwargs: Any) -> None:
        nonlocal drafted
        drafted = True
        assert destination.read_bytes() == b"new transcript\n"
        assert not store.layout.transcript_chunks_dir.exists()
        assert not store.layout.summaries_dir.exists()
        assert not pending.exists()
        assert store.read_episode_yaml()["inputs"]["transcript"] == str(destination.relative_to(store.layout.root))

    monkeypatch.setattr(draft_pipeline, "_run_llm_pipeline", verify_draft)
    transcript = {"self": destination, "identical": source, "reuse": None}[retry]
    draft_pipeline.run_draft_pipeline(
        dry_run=False,
        workspace=store.layout.root,
        episode_id="ep_001",
        transcript=transcript,
        chapters=None,
        candidates_per_asset=1,
        chunker_config=ChunkerConfig(),
        summarizer_config=StubSummarizerConfig(),
    )
    assert drafted


def test_selection_cleanup_failure_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    old = store.write_selected_text("description", TextFormat.markdown, "# A")
    before = {fmt: store.read_selected_text("description", fmt) for fmt in (TextFormat.markdown, TextFormat.html)}
    original_unlink = Path.unlink
    called = False

    def fail_unlink(path: Path, missing_ok: bool = False) -> None:
        nonlocal called
        if path == old:
            called = True
            raise OSError("injected superseded cleanup failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(OSError):
        store.write_selected_text("description", TextFormat.plain, "B")
    assert called
    for fmt, content in before.items():
        assert store.read_selected_text("description", fmt) == content
    assert not store.layout.selected_text_path("description", TextFormat.plain).exists()


def test_selection_failed_rollback_retains_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    md = store.write_selected_text("description", TextFormat.markdown, "# A")
    html = store.layout.selected_text_path("description", TextFormat.html)
    original_replace = os.replace
    failures: list[str] = []

    def fail_replace(source: str | Path, target: str | Path) -> None:
        if Path(target) == html or Path(source).suffix == ".old":
            failures.append(str(target))
            raise OSError("injected publication/rollback failure")
        original_replace(source, target)

    monkeypatch.setattr(workspace_store.os, "replace", fail_replace)
    with pytest.raises(workspace_store.WorkspaceStoreError, match="recover backups from"):
        store.write_selected_text("description", TextFormat.markdown, "# B")
    assert len(failures) == 2
    directories = list(md.parent.glob(".selection-*"))
    assert len(directories) == 1
    assert (directories[0] / (md.name + ".old")).read_text() == "# A\n"


def test_plain_to_markdown_failure_removes_new_canonical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    plain = store.write_selected_text("description", TextFormat.plain, "A")
    html = store.layout.selected_text_path("description", TextFormat.html)
    original_replace = os.replace
    called = False

    def fail_replace(source: str | Path, target: str | Path) -> None:
        nonlocal called
        if Path(target) == html:
            called = True
            raise OSError("injected derived HTML publication failure")
        original_replace(source, target)

    monkeypatch.setattr(workspace_store.os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.write_selected_text("description", TextFormat.markdown, "B")
    assert called
    assert plain.read_text() == "A\n"
    assert not store.layout.selected_text_path("description", TextFormat.markdown).exists()
    assert not html.exists()


@pytest.mark.parametrize("failure", ["invalidation", "metadata"])
def test_dashboard_draft_recovers_pending_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from podcast_pipeline.dashboard_context import BackgroundJob, DashboardContext
    from podcast_pipeline.entrypoints.dashboard_web import _run_draft_job

    store, destination, _ = _transcript_workspace(tmp_path)
    source = tmp_path / "source.txt"
    source.write_bytes(b"new transcript\n")
    called = False
    original_replace = os.replace

    def fail_clear(store: EpisodeWorkspaceStore) -> None:
        nonlocal called
        called = True
        raise OSError("injected invalidation failure")

    def fail_metadata(src: str | Path, dst: str | Path) -> None:
        nonlocal called
        if Path(dst) == store.layout.episode_yaml:
            called = True
            raise OSError("injected metadata failure")
        original_replace(src, dst)

    with monkeypatch.context() as patch:
        if failure == "invalidation":
            patch.setattr(transcript_ingest, "_clear_stale_artifacts", fail_clear)
        else:
            patch.setattr(workspace_store.os, "replace", fail_metadata)
        with pytest.raises(OSError):
            transcript_ingest.prepare_transcript(store=store, transcript=source)
    assert called
    drafted = False

    def verify_draft(**kwargs: Any) -> None:
        nonlocal drafted
        drafted = True
        assert destination.read_bytes() == b"new transcript\n"
        assert not store.layout.transcript_chunks_dir.exists()
        assert not store.layout.summaries_dir.exists()
        assert not (destination.parent / ".ingest-pending").exists()
        assert store.read_episode_yaml()["inputs"]["transcript"] == str(destination.relative_to(store.layout.root))

    monkeypatch.setattr(draft_pipeline, "_run_llm_pipeline", verify_draft)
    job = BackgroundJob(job_id="test", stage="draft")
    _run_draft_job(DashboardContext(workspace=store.layout.root), job, 1, None)
    assert drafted
    assert job.status == "completed", job.error


@pytest.mark.parametrize("already_pending", [False, True])
@pytest.mark.parametrize("failure", ["replace", "directory_flush"])
def test_ingest_marker_publication_failure_preserves_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_pending: bool, failure: str
) -> None:
    store, destination, before = _transcript_workspace(tmp_path)
    pending = destination.parent / ".ingest-pending"
    if already_pending:
        pending.write_text("pending\n")
    source = tmp_path / "source.txt"
    source.write_bytes(b"new transcript\n")
    called = False
    original_replace = os.replace

    def fail_replace(src: str | Path, dst: str | Path) -> None:
        nonlocal called
        if Path(dst) == pending:
            called = True
            raise OSError("injected marker replace failure")
        original_replace(src, dst)

    def fail_flush(path: Path) -> None:
        nonlocal called
        called = True
        assert pending.exists()
        raise OSError("injected marker directory flush failure")

    if failure == "replace":
        monkeypatch.setattr(workspace_store.os, "replace", fail_replace)
    else:
        monkeypatch.setattr(workspace_store, "_fsync_dir", fail_flush)
    with pytest.raises(OSError):
        transcript_ingest.prepare_transcript(store=store, transcript=source)
    assert called
    assert {path: path.read_bytes() for path in before} == before
    assert pending.exists() == already_pending
    assert not list(destination.parent.glob(".transcript-*"))
