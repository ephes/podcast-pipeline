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
    _render_args,
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
    assert _validate_command("podcast-transcript") == "podcast-transcript"


def test_render_args_rejects_unknown_placeholder() -> None:
    with pytest.raises(typer.BadParameter):
        _render_args(("run", "{unknown}"), mode="draft", output_dir=Path("/tmp/out"), workspace=Path("/tmp"))


def test_render_args_renders_known_placeholders(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    workspace = tmp_path / "workspace"
    args = ("--mode", "{mode}", "--output-dir", "{output_dir}", "--workspace", "{workspace}")
    rendered = _render_args(args, mode="final", output_dir=output_dir, workspace=workspace)
    assert rendered == [
        "--mode",
        "final",
        "--output-dir",
        str(output_dir),
        "--workspace",
        str(workspace),
    ]


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


def test_run_transcribe_resolves_workspace_and_appends_args(
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
        config=transcribe.TranscribeConfig(command="podcast-transcript", args=("--foo", "bar")),
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
