from __future__ import annotations

import json
import os
from pathlib import Path
from typing import IO, Any, cast

import pytest

from podcast_pipeline import workspace_store
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


@pytest.mark.parametrize("fmt", [TextFormat.plain, TextFormat.html])
def test_selection_write_failure_preserves_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: TextFormat
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_selected_text("description", TextFormat.markdown, "# A")
    selected_dir = store.layout.selected_text_path("description", TextFormat.markdown).parent
    before = {path: path.read_bytes() for path in selected_dir.iterdir()}
    assert len(before) == 2
    target = store.layout.selected_text_path("description", fmt)
    called = False
    original_replace = os.replace

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        nonlocal called
        if Path(destination) == target:
            called = True
            raise OSError("injected selection failure")
        original_replace(source, destination)

    monkeypatch.setattr(workspace_store.os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.write_selected_text("description", fmt, "B")
    assert called
    assert {path: path.read_bytes() for path in selected_dir.iterdir()} == before


def test_markdown_canonical_survives_failed_rewrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    canonical = store.write_selected_text("description", TextFormat.markdown, "# A")
    original_replace = os.replace
    replaced: list[str] = []

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        replaced.append(Path(destination).name)
        if Path(destination) == canonical:
            raise OSError("injected canonical failure")
        original_replace(source, destination)

    monkeypatch.setattr(workspace_store.os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.write_selected_text("description", TextFormat.markdown, "# B")
    assert replaced == ["description.html", "description.md"]
    assert canonical.read_text() == "# A\n"


@pytest.mark.parametrize("previous", [TextFormat.html, TextFormat.plain])
@pytest.mark.parametrize("failing", [TextFormat.markdown, TextFormat.html])
def test_switch_to_markdown_failure_preserves_previous_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous: TextFormat, failing: TextFormat
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    old = store.write_selected_text("description", previous, "<p>A</p>")
    target = store.layout.selected_text_path("description", failing)
    original_replace = os.replace
    called = False

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        nonlocal called
        if Path(destination) == target:
            called = True
            raise OSError("injected publication failure")
        original_replace(source, destination)

    monkeypatch.setattr(workspace_store.os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.write_selected_text("description", TextFormat.markdown, "# B")
    assert called
    assert [path.name for path in old.parent.iterdir()] == [old.name]
    assert old.read_text() == "<p>A</p>\n"


def test_html_to_markdown_error_after_rename_keeps_complete_new_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    html = store.write_selected_text("description", TextFormat.html, "<p>A</p>")
    original_replace = os.replace

    def replace_then_fail(source: str | Path, destination: str | Path) -> None:
        original_replace(source, destination)
        if Path(destination) == html:
            raise OSError("injected failure after rename")

    monkeypatch.setattr(workspace_store.os, "replace", replace_then_fail)
    with pytest.raises(OSError):
        store.write_selected_text("description", TextFormat.markdown, "# B")
    # The HTML was already replaced, so the Markdown it was derived from must stay.
    assert store.read_selected_text("description", TextFormat.markdown) == "# B\n"
    assert "B" in html.read_text()
    assert "<p>A</p>" not in html.read_text()


def test_selection_render_failure_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    plain = store.write_selected_text("description", TextFormat.plain, "A")

    def fail_render(content: str) -> str:
        raise ValueError("injected render failure")

    monkeypatch.setattr(workspace_store, "markdown_to_deterministic_html", fail_render)
    with pytest.raises(ValueError):
        store.write_selected_text("description", TextFormat.markdown, "# B")
    assert [path.name for path in plain.parent.iterdir()] == [plain.name]
    assert plain.read_text() == "A\n"


def test_selection_cleanup_failure_is_reported_and_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    old = store.write_selected_text("description", TextFormat.markdown, "# A")
    original_unlink = Path.unlink

    def fail_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == old:
            raise OSError("injected superseded cleanup failure")
        original_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(workspace_store.WorkspaceStoreError, match="superseded"):
            store.write_selected_text("description", TextFormat.plain, "B")
    # The superseded file is still intact; repeating the selection finishes the switch.
    assert old.read_text() == "# A\n"
    store.write_selected_text("description", TextFormat.plain, "B")
    assert [path.name for path in old.parent.iterdir()] == ["description.txt"]


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


def _snapshot(paths: dict[Path, bytes]) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in paths}


@pytest.mark.parametrize("source_kind", ["missing", "directory", "self", "hardlink"])
def test_transcript_unusable_or_self_source_preserves_current(tmp_path: Path, source_kind: str) -> None:
    store, destination, before = _transcript_workspace(tmp_path)
    source = tmp_path / "source.txt"
    if source_kind == "self":
        source = destination
    elif source_kind == "hardlink":
        source.hardlink_to(destination)
    elif source_kind == "directory":
        source.mkdir()
    if source_kind in {"missing", "directory"}:
        with pytest.raises(OSError):
            draft_pipeline._ingest_transcript(store=store, transcript=source)
    else:
        draft_pipeline._ingest_transcript(store=store, transcript=source)
    assert _snapshot(before) == before
    assert sorted(path.name for path in destination.parent.iterdir()) == ["chunks", "transcript.txt"]


@pytest.mark.parametrize("failure", ["unreadable", "copy", "metadata"])
def test_transcript_failure_before_invalidation_preserves_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    store, destination, before = _transcript_workspace(tmp_path)
    source = tmp_path / "source.txt"
    source.write_bytes(b"new transcript\n")
    called = False
    if failure == "copy":

        def fail_copy(src: IO[bytes], dst: IO[bytes]) -> None:
            nonlocal called
            called = True
            dst.write(b"partial")
            raise OSError("injected copy failure")

        monkeypatch.setattr(draft_pipeline.shutil, "copyfileobj", fail_copy)
    elif failure == "unreadable":
        original_open = Path.open

        def fail_open(path: Path, *args: Any, **kwargs: Any) -> IO[Any]:
            nonlocal called
            if path == source:
                called = True
                raise PermissionError("injected unreadable source")
            return cast(IO[Any], original_open(path, *args, **kwargs))

        monkeypatch.setattr(Path, "open", fail_open)
    else:
        called = True
        store.layout.episode_yaml.write_text("- not a mapping\n")
        before[store.layout.episode_yaml] = store.layout.episode_yaml.read_bytes()
    with pytest.raises((OSError, workspace_store.WorkspaceStoreError)):
        draft_pipeline._ingest_transcript(store=store, transcript=source)
    assert called
    assert _snapshot(before) == before
    assert sorted(path.name for path in destination.parent.iterdir()) == ["chunks", "transcript.txt"]


def test_transcript_publication_failure_never_pairs_new_transcript_with_old_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, destination, before = _transcript_workspace(tmp_path)
    source = tmp_path / "source.txt"
    source.write_bytes(b"new transcript\n")
    original_replace = os.replace
    called = False

    def fail_replace(src: str | Path, dst: str | Path) -> None:
        nonlocal called
        if Path(dst) == destination:
            called = True
            raise OSError("injected replace failure")
        original_replace(src, dst)

    monkeypatch.setattr(draft_pipeline.os, "replace", fail_replace)
    with pytest.raises(OSError):
        draft_pipeline._ingest_transcript(store=store, transcript=source)
    assert called
    # The previous transcript survives; derived artifacts are gone and get rebuilt from it.
    assert destination.read_bytes() == before[destination]
    assert store.layout.episode_yaml.read_bytes() == before[store.layout.episode_yaml]
    assert not store.layout.transcript_chunks_dir.exists()
    assert not store.layout.summaries_dir.exists()
    assert [path.name for path in destination.parent.iterdir()] == ["transcript.txt"]


def test_transcript_success_invalidates_before_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, destination, before = _transcript_workspace(tmp_path)
    destination.chmod(0o640)
    source = tmp_path / "source.txt"
    source.write_bytes(b"new transcript\n")
    original_clear = draft_pipeline._clear_stale_artifacts
    called = False

    def verify_clear(store: EpisodeWorkspaceStore) -> None:
        nonlocal called
        called = True
        assert destination.read_bytes() == before[destination]
        original_clear(store)

    monkeypatch.setattr(draft_pipeline, "_clear_stale_artifacts", verify_clear)
    draft_pipeline._ingest_transcript(store=store, transcript=source)
    assert called
    assert destination.read_bytes() == b"new transcript\n"
    assert destination.stat().st_mode & 0o777 == 0o640
    assert not store.layout.transcript_chunks_dir.exists()
    assert not store.layout.summaries_dir.exists()
    assert [path.name for path in destination.parent.iterdir()] == ["transcript.txt"]
    assert store.read_episode_yaml()["inputs"]["transcript"] == str(destination.relative_to(store.layout.root))
