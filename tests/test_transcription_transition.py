from pathlib import Path
from typing import Any

import pytest

import podcast_pipeline.entrypoints.transcribe as module
from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.entrypoints.draft_pipeline import _run_llm_pipeline
from podcast_pipeline.summarization_stub import write_episode_summary_artifacts
from podcast_pipeline.transcript_chunker import ChunkerConfig, write_transcript_chunks
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore
from tests.test_draft_pipeline import _FakeRunner


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[EpisodeWorkspaceStore, list[str | None]]:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "synthetic"})
    output: list[str | None] = ["source A", "00:00:00 Old chapter"]

    def transcriber(*, args: list[str], **kwargs: Any) -> None:
        target = Path(args[args.index("--output-dir") + 1])
        (target / "transcript.txt").write_text(output[0] or "")
        if output[1] is not None:
            (target / "chapters.txt").write_text(output[1])

    monkeypatch.setattr(module, "_run_transcriber", transcriber)
    _run(store)
    return store, output


def _run(store: EpisodeWorkspaceStore, mode: module.TranscriptionMode = module.TranscriptionMode.draft) -> None:
    module.run_transcribe(
        workspace=store.layout.root, mode=mode, config=module.TranscribeConfig(command="synthetic-transcriber")
    )


def _cache(store: EpisodeWorkspaceStore) -> dict[Path, bytes]:
    write_transcript_chunks(
        layout=store.layout, transcript_path=store.layout.transcript_dir / "transcript.txt", config=ChunkerConfig()
    )
    write_episode_summary_artifacts(layout=store.layout, episode_summary=EpisodeSummary(summary_markdown="summary A"))
    return {
        p: p.read_bytes()
        for directory in [store.layout.transcript_chunks_dir, store.layout.summaries_dir]
        for p in directory.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize(
    ("failure", "unchaptered"),
    [
        (name, absent)
        for name in ["transcript.txt", "chapters.txt", "episode.yaml", "provenance.json"]
        for absent in [False, True]
        if not (name == "chapters.txt" and absent)
    ],
)
def test_canonical_publication_failure_preserves_prior_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, unchaptered: bool
) -> None:
    store, output = _setup(tmp_path, monkeypatch)
    cached = _cache(store)
    paths = [
        store.layout.transcript_dir / "transcript.txt",
        store.layout.transcript_dir / "chapters.txt",
        store.layout.episode_yaml,
        store.layout.transcript_dir / "draft/provenance.json",
        store.layout.transcript_dir / "draft/transcript.txt",
        store.layout.transcript_dir / "draft/chapters.txt",
    ]
    before = {p: p.read_bytes() for p in paths}
    output[:] = ["source B", None if unchaptered else "00:00:00 New chapter"]
    replace = module.os.replace
    injected = []

    def fail(source: Path, target: Path) -> None:
        target = Path(target)
        wanted = next(p for p in paths if p.name == failure)
        if target == wanted and ".backup-" not in Path(source).name:
            injected.append(source)
            raise OSError("synthetic canonical replacement failure")
        replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", fail)
        with pytest.raises(OSError):
            _run(store)
    assert injected
    assert {p: p.read_bytes() for p in paths} == before
    assert all(p.read_bytes() == data for p, data in cached.items())
    _run(store)
    assert paths[0].read_text() == "source B"


def test_unchaptered_transcription_retires_only_current_mode_chapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, output = _setup(tmp_path, monkeypatch)
    _run(store, module.TranscriptionMode.final)
    final_chapters = store.layout.transcript_dir / "final/chapters.txt"
    output[:] = ["new unchaptered source", None]
    _run(store)
    inputs = store.read_episode_yaml()["inputs"]
    assert "chapters" not in inputs and "chapters_draft" not in inputs
    assert inputs["chapters_final"] == "transcript/final/chapters.txt"
    assert final_chapters.read_text() == "00:00:00 Old chapter"
    assert not (store.layout.transcript_dir / "chapters.txt").exists()


@pytest.mark.parametrize("changed", [False, True])
def test_transcription_content_identity_controls_draft_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    import podcast_pipeline.drafter_runner as runner_module

    store, output = _setup(tmp_path, monkeypatch)
    cached = _cache(store)
    output[:] = ["source B" if changed else "source A", None]
    _run(store)
    fake = _FakeRunner()
    monkeypatch.setattr(runner_module, "DrafterCliRunner", lambda **kwargs: fake)
    _run_llm_pipeline(store=store, candidates_per_asset=1, chunker_config=ChunkerConfig(), timeout_seconds=None)
    summary = store.layout.episode_summary_json_path().read_text()
    assert ("summary A" not in summary) if changed else ("summary A" in summary)
    prompts = "\n".join(fake.prompts)
    if changed:
        assert "source B" in prompts and "summary A" not in prompts and "source A" not in prompts
    else:
        assert all(p.read_bytes() == data for p, data in cached.items())
        assert not any("Transcript chunk:" in prompt for prompt in fake.prompts)


def test_manual_chapter_import_is_retired_with_new_unchaptered_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from podcast_pipeline.entrypoints.draft_pipeline import _copy_chapters_into_workspace

    store, output = _setup(tmp_path, monkeypatch)
    output[1] = None
    _run(store)
    assert "chapters_draft" not in store.read_episode_yaml()["inputs"]
    source = tmp_path / "manual-source.txt"
    source.write_text("00:00:01 Manual chapter")
    _copy_chapters_into_workspace(workspace=tmp_path, chapters=source)
    output[:] = ["new source", None]
    _run(store)
    assert source.read_text() == "00:00:01 Manual chapter"
    assert not (store.layout.transcript_dir / "chapters.txt").exists()
    assert "chapters" not in store.read_episode_yaml()["inputs"]
    assert "chapters_draft" not in store.read_episode_yaml()["inputs"]


def test_builtin_transcriber_does_not_republish_stale_mode_chapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _output = _setup(tmp_path, monkeypatch)
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(b"invented audio bytes")
    with store.update_episode_yaml() as data:
        data["inputs"]["transcription_audio"] = audio.name

    def transcriber(*, env: dict[str, str], **kwargs: Any) -> None:
        directory = Path(env["TRANSCRIPT_DIR"]) / audio.stem
        directory.mkdir(parents=True)
        (directory / f"{audio.stem}.txt").write_text("new built-in source")

    monkeypatch.setattr(module, "_run_transcriber", transcriber)
    module.run_transcribe(workspace=tmp_path, mode=module.TranscriptionMode.draft, config=module.TranscribeConfig())
    assert (store.layout.transcript_dir / "transcript.txt").read_text().strip() == "new built-in source"
    assert not (store.layout.transcript_dir / "chapters.txt").exists()
    assert not (store.layout.transcript_dir / "draft/chapters.txt").exists()
    assert "chapters" not in store.read_episode_yaml()["inputs"]
