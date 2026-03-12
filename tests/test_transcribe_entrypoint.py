from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import podcast_pipeline.entrypoints.transcribe as transcribe
from podcast_pipeline.entrypoints.cli import app
from podcast_pipeline.entrypoints.transcribe import (
    _args_need_audio_file,
    _default_args_for_command,
    _podcast_transcript_plaintext_path,
    _render_args,
    _resolve_audio_file,
    _update_episode_inputs,
    _validate_command,
    _write_transcribe_provenance,
)
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def test_cli_transcribe_rejects_invalid_mode(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = runner.invoke(
        app,
        [
            "transcribe",
            "--workspace",
            str(workspace),
            "--mode",
            "invalid",
        ],
    )

    assert result.exit_code != 0
    output = result.stdout + result.stderr
    assert "mode must be 'draft' or 'final'" in output


@pytest.mark.parametrize("command", ["", " ", "podcast transcript"])
def test_validate_command_rejects_invalid_inputs(command: str) -> None:
    with pytest.raises(typer.BadParameter):
        _validate_command(command)


def test_validate_command_accepts_single_token_command() -> None:
    assert _validate_command("transcribe") == "transcribe"


def test_render_args_rejects_unknown_placeholder() -> None:
    with pytest.raises(typer.BadParameter):
        _render_args(
            ("run", "{unknown}"),
            mode="draft",
            output_dir=Path("/tmp/out"),
            workspace=Path("/tmp"),
            audio_file=Path("/tmp/audio.mp3"),
        )


def test_render_args_renders_known_placeholders(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    workspace = tmp_path / "workspace"
    audio_file = tmp_path / "audio.mp3"
    args = ("--mode", "{mode}", "--output-dir", "{output_dir}", "--workspace", "{workspace}", "{audio_file}")
    rendered = _render_args(
        args,
        mode="final",
        output_dir=output_dir,
        workspace=workspace,
        audio_file=audio_file,
    )
    assert rendered == [
        "--mode",
        "final",
        "--output-dir",
        str(output_dir),
        "--workspace",
        str(workspace),
        str(audio_file),
    ]


def test_default_args_use_audio_file_for_transcribe_command() -> None:
    assert _default_args_for_command("transcribe") == ("{audio_file}",)


def test_default_args_keep_legacy_contract_for_other_commands() -> None:
    assert _default_args_for_command("custom-transcriber") == ("--mode", "{mode}", "--output-dir", "{output_dir}")


def test_args_need_audio_file_detects_placeholder() -> None:
    assert _args_need_audio_file(("{audio_file}", "--backend", "voxhelm")) is True


def test_args_need_audio_file_ignores_legacy_templates() -> None:
    assert _args_need_audio_file(("--mode", "{mode}", "--output-dir", "{output_dir}")) is False


def test_resolve_audio_file_prefers_auphonic_input_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    audio_file = workspace / "mix.mp3"
    audio_file.write_bytes(b"audio")

    resolved = _resolve_audio_file(
        episode_yaml={"episode_id": "ep_001", "auphonic": {"input_file": "mix.mp3"}},
        workspace=workspace,
    )

    assert resolved == audio_file.resolve()


def test_resolve_audio_file_accepts_single_auphonic_input_files_entry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    audio_file = workspace / "mix.mp3"
    audio_file.write_bytes(b"audio")

    resolved = _resolve_audio_file(
        episode_yaml={"episode_id": "ep_001", "auphonic": {"input_files": ["mix.mp3"]}},
        workspace=workspace,
    )

    assert resolved == audio_file.resolve()


def test_resolve_audio_file_uses_single_preferred_track(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    mix_path = media_dir / "mix.wav"
    mix_path.write_bytes(b"audio")
    dialog_path = media_dir / "dialog.wav"
    dialog_path.write_bytes(b"audio")

    resolved = _resolve_audio_file(
        episode_yaml={
            "episode_id": "ep_001",
            "sources": {"reaper_media_dir": str(media_dir)},
            "tracks": [
                {"path": "mix.wav", "role": "mix"},
                {"path": "dialog.wav", "role": "dialog"},
            ],
        },
        workspace=workspace,
    )

    assert resolved == mix_path.resolve()


def test_resolve_audio_file_rejects_multiple_preferred_tracks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "mix_a.wav").write_bytes(b"audio")
    (media_dir / "mix_b.wav").write_bytes(b"audio")

    with pytest.raises(typer.BadParameter, match="multiple preferred"):
        _resolve_audio_file(
            episode_yaml={
                "episode_id": "ep_001",
                "sources": {"reaper_media_dir": str(media_dir)},
                "tracks": [
                    {"path": "mix_a.wav", "role": "mix"},
                    {"path": "mix_b.wav", "role": "final"},
                ],
            },
            workspace=workspace,
        )


def test_resolve_audio_file_rejects_missing_audio_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(typer.BadParameter, match="Could not determine a single source audio file"):
        _resolve_audio_file(
            episode_yaml={"episode_id": "ep_001"},
            workspace=workspace,
        )


def test_update_episode_inputs_writes_transcript_paths(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    episode_yaml = {"episode_id": "ep_001", "inputs": {"preexisting": "value"}}

    mode_transcript = tmp_path / "transcript" / "draft" / "transcript.txt"
    mode_chapters = tmp_path / "transcript" / "draft" / "chapters.txt"
    default_transcript = tmp_path / "transcript" / "transcript.txt"
    default_chapters = tmp_path / "transcript" / "chapters.txt"

    _update_episode_inputs(
        store=store,
        episode_yaml=episode_yaml,
        mode="draft",
        mode_transcript=mode_transcript,
        mode_chapters=mode_chapters,
        default_transcript=default_transcript,
        default_chapters=default_chapters,
    )

    updated = store.read_episode_yaml()
    inputs = updated["inputs"]
    assert inputs["preexisting"] == "value"
    assert inputs["transcript_draft"] == "transcript/draft/transcript.txt"
    assert inputs["transcript"] == "transcript/transcript.txt"
    assert inputs["chapters_draft"] == "transcript/draft/chapters.txt"
    assert inputs["chapters"] == "transcript/chapters.txt"


def test_write_transcribe_provenance_includes_outputs(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    episode_yaml = {
        "episode_id": "ep_001",
        "sources": {"reaper_media_dir": "/tmp/reaper"},
        "tracks": [{"track_id": "host_main", "path": "Mic A.flac"}],
    }

    mode_dir = tmp_path / "transcript" / "draft"
    mode_dir.mkdir(parents=True)
    (mode_dir / "chapters.txt").write_text("00:00 Intro\n", encoding="utf-8")

    default_transcript = tmp_path / "transcript" / "transcript.txt"
    default_chapters = tmp_path / "transcript" / "chapters.txt"

    _write_transcribe_provenance(
        store=store,
        mode="draft",
        mode_dir=mode_dir,
        command="podcast-transcript",
        args=["--mode", "draft"],
        default_transcript=default_transcript,
        default_chapters=default_chapters,
        episode_yaml=episode_yaml,
    )

    provenance_path = mode_dir / "provenance.json"
    payload = json.loads(provenance_path.read_text(encoding="utf-8"))

    assert payload["version"] == 1
    assert payload["mode"] == "draft"
    assert payload["command"] == "podcast-transcript"
    assert payload["args"] == ["--mode", "draft"]
    assert payload["episode_id"] == "ep_001"
    assert payload["sources"] == {"reaper_media_dir": "/tmp/reaper"}
    assert payload["tracks"] == [{"track_id": "host_main", "path": "Mic A.flac"}]

    outputs = payload["outputs"]
    assert outputs["mode_dir"] == "transcript/draft"
    assert outputs["mode_transcript"] == "transcript/draft/transcript.txt"
    assert outputs["default_transcript"] == "transcript/transcript.txt"
    assert outputs["mode_chapters"] == "transcript/draft/chapters.txt"
    assert outputs["default_chapters"] == "transcript/chapters.txt"

    created_at = payload["created_at"]
    assert isinstance(created_at, str) and created_at
    datetime.fromisoformat(created_at)


def test_run_transcriber_wraps_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="podcast-transcript", timeout=12.5)

    monkeypatch.setattr(transcribe.subprocess, "run", fake_run)

    with pytest.raises(typer.BadParameter) as exc:
        transcribe._run_transcriber(
            command="podcast-transcript",
            args=[],
            cwd=tmp_path,
            timeout_seconds=12.5,
        )

    assert "timed out" in str(exc.value)


def test_run_transcribe_keeps_legacy_wrapper_args_for_custom_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "episode.yaml").write_text("schema_version: 1\nepisode_id: ep_001\n", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_run_transcriber(
        *,
        command: str,
        args: list[str],
        cwd: Path,
        timeout_seconds: float | None,
    ) -> None:
        captured["command"] = command
        captured["args"] = args
        captured["cwd"] = cwd
        output_index = args.index("--output-dir") + 1
        output_dir = Path(args[output_index])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "transcript.txt").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(transcribe, "_run_transcriber", fake_run_transcriber)
    monkeypatch.chdir(tmp_path)

    transcribe.run_transcribe(
        workspace=Path("workspace"),
        mode=transcribe.TranscriptionMode.draft,
        config=transcribe.TranscribeConfig(command="custom-transcriber", args=("--foo", "bar")),
    )

    args = captured["args"]
    assert isinstance(args, list)
    assert args[:4] == [
        "--mode",
        "draft",
        "--output-dir",
        str(workspace.resolve() / "transcript" / "draft"),
    ]
    assert args[4:] == ["--foo", "bar"]
    assert captured["cwd"] == workspace.resolve()


def test_run_transcribe_skips_audio_resolution_for_custom_commands_without_audio_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "episode.yaml").write_text("schema_version: 1\nepisode_id: ep_001\n", encoding="utf-8")

    def fake_run_transcriber(
        *,
        command: str,
        args: list[str],
        cwd: Path,
        timeout_seconds: float | None,
    ) -> None:
        output_dir = workspace / "transcript" / "draft"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "transcript.txt").write_text("legacy custom output", encoding="utf-8")

    monkeypatch.setattr(transcribe, "_run_transcriber", fake_run_transcriber)

    transcribe.run_transcribe(
        workspace=workspace,
        mode=transcribe.TranscriptionMode.draft,
        config=transcribe.TranscribeConfig(command="custom-transcriber"),
    )

    assert (workspace / "transcript" / "draft" / "transcript.txt").read_text(encoding="utf-8") == (
        "legacy custom output"
    )


def test_run_transcribe_keeps_direct_workspace_output_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    audio_file = workspace / "mix.mp3"
    audio_file.write_bytes(b"audio")
    (workspace / "episode.yaml").write_text(
        "schema_version: 1\nepisode_id: ep_001\nauphonic:\n  input_file: mix.mp3\n",
        encoding="utf-8",
    )

    transcript_dir = tmp_path / "podcast-transcripts"
    monkeypatch.setenv("TRANSCRIPT_DIR", str(transcript_dir))

    def fake_run_transcriber(
        *,
        command: str,
        args: list[str],
        cwd: Path,
        timeout_seconds: float | None,
    ) -> None:
        output_dir = workspace / "transcript" / "draft"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "transcript.txt").write_text("workspace wins", encoding="utf-8")

        output_path = _podcast_transcript_plaintext_path(audio_file.resolve())
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("external transcript", encoding="utf-8")

    monkeypatch.setattr(transcribe, "_run_transcriber", fake_run_transcriber)

    transcribe.run_transcribe(
        workspace=workspace,
        mode=transcribe.TranscriptionMode.draft,
        config=transcribe.TranscribeConfig(command="transcribe"),
    )

    assert (workspace / "transcript" / "draft" / "transcript.txt").read_text(encoding="utf-8") == "workspace wins"


def test_run_transcribe_imports_podcast_transcript_plaintext_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    audio_file = workspace / "mix.mp3"
    audio_file.write_bytes(b"audio")
    (workspace / "episode.yaml").write_text(
        "schema_version: 1\nepisode_id: ep_001\nauphonic:\n  input_file: mix.mp3\n",
        encoding="utf-8",
    )

    transcript_dir = tmp_path / "podcast-transcripts"
    monkeypatch.setenv("TRANSCRIPT_DIR", str(transcript_dir))

    def fake_run_transcriber(
        *,
        command: str,
        args: list[str],
        cwd: Path,
        timeout_seconds: float | None,
    ) -> None:
        output_path = _podcast_transcript_plaintext_path(audio_file.resolve())
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("hello from podcast-transcript", encoding="utf-8")

    monkeypatch.setattr(transcribe, "_run_transcriber", fake_run_transcriber)

    transcribe.run_transcribe(
        workspace=workspace,
        mode=transcribe.TranscriptionMode.final,
        config=transcribe.TranscribeConfig(command="transcribe"),
    )

    assert (workspace / "transcript" / "final" / "transcript.txt").read_text(encoding="utf-8") == (
        "hello from podcast-transcript"
    )
