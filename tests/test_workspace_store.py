from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from podcast_pipeline import workspace_store
from podcast_pipeline.domain.models import (
    Candidate,
    EpisodeWorkspace,
    ProvenanceRef,
    ReviewIteration,
    ReviewVerdict,
    TextFormat,
)
from podcast_pipeline.workspace_store import (
    EpisodeWorkspaceLayout,
    EpisodeWorkspaceStore,
    WorkspaceStoreError,
    episode_workspace_dir,
    episodes_dir,
)


def test_layout_paths(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    assert layout.episode_yaml == tmp_path / "episode.yaml"
    assert layout.state_json == tmp_path / "state.json"
    assert layout.transcript_dir == tmp_path / "transcript"
    assert layout.transcript_chunks_dir == tmp_path / "transcript" / "chunks"
    assert layout.summaries_dir == tmp_path / "summaries"
    assert layout.chunk_summaries_dir == tmp_path / "summaries" / "chunks"
    assert layout.episode_summary_dir == tmp_path / "summaries" / "episode"
    assert layout.copy_candidates_dir == tmp_path / "copy" / "candidates"
    assert layout.copy_reviews_dir == tmp_path / "copy" / "reviews"
    assert layout.copy_selected_dir == tmp_path / "copy" / "selected"
    assert layout.copy_provenance_dir == tmp_path / "copy" / "provenance"


def test_episode_workspace_dir_defaults_to_episodes_folder(tmp_path: Path) -> None:
    assert episodes_dir(tmp_path) == tmp_path / "episodes"
    assert episode_workspace_dir(tmp_path, "pp_068") == tmp_path / "episodes" / "pp_068"


def test_layout_copy_paths_are_deterministic(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)

    candidate_id = UUID("01234567-89ab-cdef-0123-456789abcdef")
    expected_candidate_json = tmp_path / "copy" / "candidates" / "description" / f"candidate_{candidate_id}.json"
    assert layout.candidate_json_path("description", candidate_id) == expected_candidate_json
    expected_candidate_md = tmp_path / "copy" / "candidates" / "description" / f"candidate_{candidate_id}.md"
    assert layout.candidate_text_path("description", candidate_id, TextFormat.markdown) == expected_candidate_md

    assert layout.review_iteration_json_path("description", 3, reviewer="reviewer_a") == (
        tmp_path / "copy" / "reviews" / "description" / "iteration_03.reviewer_a.json"
    )
    assert layout.creator_iteration_json_path("description", 4) == (
        tmp_path / "copy" / "protocol" / "description" / "iteration_04.creator.json"
    )

    assert layout.selected_text_path("description", TextFormat.markdown) == (
        tmp_path / "copy" / "selected" / "description.md"
    )

    assert layout.provenance_json_path("codex", "run_001") == (
        tmp_path / "copy" / "provenance" / "codex" / "run_001.json"
    )

    assert layout.transcript_chunk_text_path(1) == tmp_path / "transcript" / "chunks" / "chunk_0001.txt"
    assert layout.transcript_chunk_meta_json_path(1) == tmp_path / "transcript" / "chunks" / "chunk_0001.json"
    assert layout.chunk_summary_json_path(1) == tmp_path / "summaries" / "chunks" / "chunk_0001.summary.json"

    assert layout.episode_summary_json_path() == tmp_path / "summaries" / "episode" / "episode_summary.json"
    assert layout.episode_summary_markdown_path() == tmp_path / "summaries" / "episode" / "episode_summary.md"
    assert layout.episode_summary_html_path() == tmp_path / "summaries" / "episode" / "episode_summary.html"


def test_store_reads_writes_episode_yaml(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep_001", "sources": {"reaper_media_dir": "/tmp"}})
    loaded = store.read_episode_yaml()
    assert loaded["episode_id"] == "ep_001"
    assert loaded["sources"]["reaper_media_dir"] == "/tmp"


def test_short_episode_yaml_stage_preserves_canonical_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    original = {"schema_version": 1, "episode_id": "synthetic", "hosts": ["Synthetic host"]}
    store.write_episode_yaml(original)
    canonical = store.layout.episode_yaml
    before = canonical.read_bytes()
    named_tempfile = workspace_store.tempfile.NamedTemporaryFile

    def short_stage(**kwargs: Any) -> Any:
        stage = named_tempfile(**kwargs)
        write = stage.write
        monkeypatch.setattr(stage, "write", lambda data: write(data[: len(data) // 2]))
        return stage

    monkeypatch.setattr(workspace_store.tempfile, "NamedTemporaryFile", short_stage)
    with pytest.raises(OSError, match="Short write"):
        with store.update_episode_yaml() as episode:
            episode["hosts"] = ["Updated host"]

    assert canonical.read_bytes() == before
    assert store.read_episode_yaml() == original
    assert not list(tmp_path.glob(".write-*.tmp"))


def test_episode_yaml_duplicate_track_setting_refuses_update(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    path = store.layout.episode_yaml
    raw = (
        "episode_id: synthetic\n"
        "auphonic:\n"
        "  tracks:\n"
        "    - settings:\n"
        "        synthetic_setting: true\n"
        "        synthetic_setting: false\n"
    )
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(WorkspaceStoreError, match=r"Duplicate YAML key.*line 6, column 9") as error:
        with store.update_episode_yaml() as data:
            data["hosts"] = ["Synthetic host"]
    assert "synthetic_setting" not in str(error.value)
    assert path.read_text(encoding="utf-8") == raw


def test_episode_yaml_preserves_legacy_merge_precedence(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.layout.episode_yaml.write_text(
        "episode_id: synthetic\n"
        "defaults: &defaults {synthetic_setting: true, other_setting: true}\n"
        "auphonic:\n"
        "  tracks:\n"
        "    - settings: {<<: *defaults, synthetic_setting: false}\n",
        encoding="utf-8",
    )
    assert store.read_episode_yaml()["auphonic"]["tracks"][0]["settings"] == {
        "synthetic_setting": False,
        "other_setting": True,
    }


@pytest.mark.parametrize(
    ("extension", "limit", "maximum", "error"),
    [
        (
            "a: &a [1, 2]\nb: &b [*a, *a]\nc: &c [*b, *b]\nd: [*c, *c]\n",
            "_EPISODE_YAML_MAX_NODES",
            40,
            "node limit",
        ),
        ("extra: [[[[1]]]]\n", "_EPISODE_YAML_MAX_DEPTH", 4, "depth limit"),
        ("extra: &cycle [*cycle]\n", "_EPISODE_YAML_MAX_DEPTH", 64, "alias cycle"),
    ],
)
def test_episode_yaml_refuses_excessive_graph_before_hydration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extension: str, limit: str, maximum: int, error: str
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    path = store.layout.episode_yaml
    raw = "episode_id: synthetic\n" + extension
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(workspace_store, limit, maximum, raising=False)

    def unexpected_hydration(raw: object) -> None:
        pytest.fail("episode YAML was hydrated before admission")

    monkeypatch.setattr(workspace_store, "try_load_episode_yaml", unexpected_hydration)
    with pytest.raises(WorkspaceStoreError, match=error):
        with store.update_episode_yaml():
            pytest.fail("episode YAML update reached its body")
    assert path.read_text(encoding="utf-8") == raw


def test_store_reads_writes_state_json(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    created_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    workspace = EpisodeWorkspace(episode_id="ep_001", root_dir=str(tmp_path), created_at=created_at)
    store.write_state(workspace)
    loaded = store.read_state()
    assert loaded.model_dump(mode="json") == workspace.model_dump(mode="json")


def test_store_reads_writes_copy_artifacts(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    created_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)

    candidate_id = UUID("01234567-89ab-cdef-0123-456789abcdef")
    candidate = Candidate(
        candidate_id=candidate_id,
        asset_id="description",
        format=TextFormat.markdown,
        content="# Hello\n",
        created_at=created_at,
    )
    candidate_path = store.write_candidate(candidate)
    assert candidate_path.exists()
    assert store.layout.candidate_text_path("description", candidate_id, TextFormat.markdown).exists()
    assert store.layout.candidate_text_path("description", candidate_id, TextFormat.html).exists()
    loaded_candidate = store.read_candidate("description", candidate_id).model_dump(mode="json")
    assert loaded_candidate == candidate.model_dump(mode="json")

    review = ReviewIteration(iteration=1, verdict=ReviewVerdict.ok, reviewer="reviewer_a", created_at=created_at)
    review_path = store.write_review("description", review)
    assert review_path.exists()
    loaded_review = store.read_review("description", 1, reviewer="reviewer_a").model_dump(mode="json")
    assert loaded_review == review.model_dump(mode="json")

    selected_path = store.write_selected_text("description", TextFormat.markdown, "final\n")
    assert selected_path.exists()
    assert store.layout.selected_text_path("description", TextFormat.html).exists()
    assert store.read_selected_text("description", TextFormat.markdown) == "final\n"

    provenance = ProvenanceRef(kind="codex", ref="run_001", created_at=created_at)
    provenance_path = store.write_provenance_json(provenance, {"ok": True})
    assert provenance_path.exists()


@pytest.mark.parametrize("clear", [False, True], ids=["change-format", "clear"])
@pytest.mark.parametrize("fail_sync", [False, True], ids=["success", "sync-failure"])
def test_selection_syncs_removed_formats_before_state_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clear: bool, fail_sync: bool
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    workspace = EpisodeWorkspace(episode_id="ep_001", root_dir=str(tmp_path))
    store.write_state(workspace)
    state_before = store.layout.state_json.read_bytes()
    store.write_selected_text("description", TextFormat.markdown, "# Before")
    paths = {fmt: store.layout.selected_text_path("description", fmt) for fmt in TextFormat}
    before = {fmt: paths[fmt].read_bytes() for fmt in (TextFormat.markdown, TextFormat.html)}
    expected = {} if clear else {TextFormat.plain: b"After\n"}
    fsync_dir = workspace_store._fsync_dir
    synced: list[Path] = []

    def sync(path: Path, *, required: bool = False) -> None:
        if path == store.layout.copy_selected_dir and required:
            files = {fmt: selected.read_bytes() for fmt, selected in paths.items() if selected.exists()}
            if files == expected:
                assert store.layout.state_json.read_bytes() == state_before
                synced.append(path)
                if fail_sync:
                    raise OSError("synthetic selection deletion sync failure")
        fsync_dir(path, required=required)

    def update(current: EpisodeWorkspace) -> EpisodeWorkspace:
        if clear:
            store.clear_selected_text("description")
        else:
            store.write_selected_text("description", TextFormat.plain, "After")
        return current.model_copy(update={"episode_id": "ep_updated"})

    monkeypatch.setattr(workspace_store, "_fsync_dir", sync)
    if fail_sync:
        with pytest.raises(WorkspaceStoreError, match="synthetic selection deletion sync failure"):
            store.update_state(update, default=workspace, artifact_paths=list(paths.values()))
        assert store.layout.state_json.read_bytes() == state_before
    else:
        store.update_state(update, default=workspace, artifact_paths=list(paths.values()))
        assert store.read_state().episode_id == "ep_updated"
    assert synced == [store.layout.copy_selected_dir]
    assert {fmt: path.read_bytes() for fmt, path in paths.items() if path.exists()} == (
        before if fail_sync else expected
    )


def test_store_rejects_invalid_review_json(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    path = store.layout.review_iteration_json_path("description", 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"iteration": 1, "verdict": "nope"}\n', encoding="utf-8")

    with pytest.raises(WorkspaceStoreError):
        store.read_review("description", 1)


def test_layout_rejects_path_separators(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    with pytest.raises(ValueError):
        layout.candidate_json_path("a/b", UUID("01234567-89ab-cdef-0123-456789abcdef"))


@pytest.mark.parametrize("existing", [False, True], ids=["create", "rewrite"])
@pytest.mark.parametrize("failure", ["render", "md", "html", "json"])
def test_candidate_failure_restores_complete_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool, failure: str
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    candidate = Candidate(asset_id="description", format=TextFormat.markdown, content="# Before")
    if existing:
        store.write_candidate(candidate)
    paths = [
        store.layout.candidate_json_path(candidate.asset_id, candidate.candidate_id),
        store.layout.candidate_text_path(candidate.asset_id, candidate.candidate_id, TextFormat.markdown),
        store.layout.candidate_text_path(candidate.asset_id, candidate.candidate_id, TextFormat.html),
    ]
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    replace = workspace_store.os.replace
    failed = False

    def fail_replace(source: str | Path, dest: str | Path) -> None:
        nonlocal failed
        # Fail once so recovery itself is available; include failure after replacement.
        if not failed and str(dest).endswith("." + failure):
            failed = True
            replace(source, dest)
            raise OSError("synthetic publication failure")
        replace(source, dest)

    def fail_render(content: str) -> str:
        raise ValueError("synthetic render failure")

    if failure == "render":
        monkeypatch.setattr(workspace_store, "markdown_to_deterministic_html", fail_render)
    else:
        monkeypatch.setattr(workspace_store.os, "replace", fail_replace)
    with pytest.raises((OSError, ValueError), match="synthetic"):
        store.write_candidate(candidate.model_copy(update={"content": "# After"}))
    assert {path: path.read_bytes() if path.exists() else None for path in paths} == before
    assert not list(tmp_path.rglob(".backup-*"))


def test_candidate_json_is_published_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    candidate = Candidate(asset_id="description", format=TextFormat.markdown, content="# Hello")
    write = workspace_store._atomic_write_text
    published: list[str] = []

    def record(path: Path, text: str) -> None:
        write(path, text)
        published.append(path.suffix)

    monkeypatch.setattr(workspace_store, "_atomic_write_text", record)
    store.write_candidate(candidate)
    assert published == [".md", ".html", ".json"]


@pytest.mark.parametrize("missing_format", [TextFormat.markdown, TextFormat.html])
def test_identical_immutable_candidate_retry_repairs_missing_companion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_format: TextFormat
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    candidate = Candidate(asset_id="description", format=TextFormat.markdown, content="# Synthetic copy")
    json_path = store.write_candidate(candidate, immutable=True)
    missing = store.layout.candidate_text_path(candidate.asset_id, candidate.candidate_id, missing_format)
    expected = missing.read_bytes()
    missing.unlink()
    write = workspace_store._atomic_write_text
    published: list[Path] = []

    def record(path: Path, text: str) -> None:
        write(path, text)
        published.append(path)

    monkeypatch.setattr(workspace_store, "_atomic_write_text", record)
    assert store.write_candidate(candidate, immutable=True) == json_path
    assert missing.read_bytes() == expected
    assert store.write_candidate(candidate, immutable=True) == json_path
    assert published == [missing]


def test_immutable_candidate_retry_rejects_conflicting_companion_before_repair(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    candidate = Candidate(asset_id="description", format=TextFormat.markdown, content="# Synthetic copy")
    json_path = store.write_candidate(candidate, immutable=True)
    markdown = store.layout.candidate_text_path(candidate.asset_id, candidate.candidate_id, TextFormat.markdown)
    html = store.layout.candidate_text_path(candidate.asset_id, candidate.candidate_id, TextFormat.html)
    json_before = json_path.read_bytes()
    markdown.unlink()
    conflict = html.read_bytes().replace(b"\n", b"\r\n")
    html.write_bytes(conflict)

    with pytest.raises(WorkspaceStoreError, match="Immutable candidate companion conflicts") as error:
        store.write_candidate(candidate, immutable=True)
    assert str(html) in str(error.value)
    assert html.read_bytes() == conflict
    assert not markdown.exists()
    assert json_path.read_bytes() == json_before


@pytest.mark.parametrize("mismatch", ["candidate_id", "asset_id"])
def test_read_candidate_binds_embedded_identity_to_requested_path(tmp_path: Path, mismatch: str) -> None:
    from uuid import uuid4

    store = EpisodeWorkspaceStore(tmp_path)
    requested_id = uuid4()
    candidate = Candidate(
        candidate_id=uuid4() if mismatch == "candidate_id" else requested_id,
        asset_id="shownotes" if mismatch == "asset_id" else "description",
        content="Synthetic copy",
    )
    path = store.layout.candidate_json_path("description", requested_id)
    path.parent.mkdir(parents=True)
    path.write_text(candidate.model_dump_json())
    before = path.read_bytes()
    with pytest.raises(WorkspaceStoreError, match="Candidate identity does not match requested path"):
        store.read_candidate("description", requested_id)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("iteration", "reviewer", "requested_reviewer"),
    [
        (2, "reviewer_a", "reviewer_a"),
        (1, "reviewer_b", "reviewer_a"),
        (1, "reviewer_a", None),
        (1, None, "reviewer_a"),
    ],
)
def test_read_review_binds_embedded_identity_to_requested_path(
    tmp_path: Path, iteration: int, reviewer: str | None, requested_reviewer: str | None
) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    review = ReviewIteration(iteration=iteration, reviewer=reviewer, verdict=ReviewVerdict.ok)
    path = store.layout.review_iteration_json_path("description", 1, reviewer=requested_reviewer)
    path.parent.mkdir(parents=True)
    path.write_text(review.model_dump_json())
    before = path.read_bytes()
    with pytest.raises(WorkspaceStoreError, match="Review identity does not match requested path"):
        store.read_review("description", 1, reviewer=requested_reviewer)
    assert path.read_bytes() == before


def test_read_artifacts_accept_matching_legacy_optional_metadata(tmp_path: Path) -> None:
    import json
    from uuid import uuid4

    store = EpisodeWorkspaceStore(tmp_path)
    candidate_id = uuid4()
    path = store.layout.candidate_json_path("description", candidate_id)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"candidate_id": str(candidate_id), "asset_id": "description", "content": "Old copy"}))
    candidate = store.read_candidate("description", candidate_id)
    assert candidate.provenance == []
    assert candidate.format == TextFormat.markdown
    path = store.layout.review_iteration_json_path("description", 1)
    path.parent.mkdir(parents=True)
    path.write_text('{"iteration":1,"verdict":"ok"}')
    assert store.read_review("description", 1).reviewer is None


def test_workspace_replacement_and_rollback_support_maximum_basename(tmp_path: Path) -> None:
    path = tmp_path / ("x" * workspace_store.os.pathconf(tmp_path, "PC_NAME_MAX"))
    path.write_text("original")
    path.chmod(0o640)

    workspace_store.atomic_write_text(path, "published")
    assert path.read_text() == "published"
    assert path.stat().st_mode & 0o777 == 0o640

    with pytest.raises(ValueError, match="synthetic publication failure"):
        with workspace_store.preserve_files([path]):
            workspace_store.atomic_write_text(path, "uncommitted")
            path.chmod(0o600)
            raise ValueError("synthetic publication failure")

    assert path.read_text() == "published"
    assert path.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.iterdir()) == [path]
