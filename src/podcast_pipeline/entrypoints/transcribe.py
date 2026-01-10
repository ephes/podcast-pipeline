from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer

from podcast_pipeline.workspace_store import EpisodeWorkspaceStore, WorkspaceStoreError, atomic_write_text


class TranscriptionMode(StrEnum):
    draft = "draft"
    final = "final"


_DEFAULT_TRANSCRIBE_ARGS: tuple[str, ...] = ("--mode", "{mode}", "--output-dir", "{output_dir}")


@dataclass(frozen=True)
class TranscribeConfig:
    command: str = "podcast-transcript"
    args: tuple[str, ...] | None = None
    timeout_seconds: float | None = None


def run_transcribe(
    *,
    workspace: Path,
    mode: TranscriptionMode,
    config: TranscribeConfig,
) -> None:
    workspace = workspace.expanduser()
    if not workspace.exists():
        raise typer.BadParameter(f"workspace does not exist: {workspace}")
    if not workspace.is_dir():
        raise typer.BadParameter(f"workspace is not a directory: {workspace}")
    workspace = workspace.resolve()

    store = EpisodeWorkspaceStore(workspace)
    try:
        episode_yaml = store.read_episode_yaml()
    except FileNotFoundError as exc:
        raise typer.BadParameter(f"episode.yaml not found in {workspace}") from exc
    except WorkspaceStoreError as exc:
        raise typer.BadParameter(str(exc)) from exc

    command = _validate_command(config.command)
    transcript_root = store.layout.transcript_dir
    transcript_root.mkdir(parents=True, exist_ok=True)
    mode_dir = transcript_root / mode.value
    mode_dir.mkdir(parents=True, exist_ok=True)

    if config.args is None:
        args = _DEFAULT_TRANSCRIBE_ARGS
    else:
        args = _DEFAULT_TRANSCRIBE_ARGS + config.args
    rendered_args = _render_args(
        args,
        mode=mode.value,
        output_dir=mode_dir,
        workspace=workspace,
    )
    _run_transcriber(
        command=command,
        args=rendered_args,
        cwd=workspace,
        timeout_seconds=config.timeout_seconds,
    )

    mode_transcript = mode_dir / "transcript.txt"
    if not mode_transcript.exists():
        raise typer.BadParameter(f"Missing transcript output at {mode_transcript}")
    if mode_transcript.stat().st_size == 0:
        raise typer.BadParameter(f"Transcript output is empty: {mode_transcript}")

    mode_chapters = mode_dir / "chapters.txt"
    default_transcript = transcript_root / "transcript.txt"
    shutil.copyfile(mode_transcript, default_transcript)

    default_chapters: Path | None = None
    if mode_chapters.exists():
        default_chapters = transcript_root / "chapters.txt"
        shutil.copyfile(mode_chapters, default_chapters)

    _update_episode_inputs(
        store=store,
        episode_yaml=episode_yaml,
        mode=mode.value,
        mode_transcript=mode_transcript,
        mode_chapters=mode_chapters if mode_chapters.exists() else None,
        default_transcript=default_transcript,
        default_chapters=default_chapters,
    )

    _write_transcribe_provenance(
        store=store,
        mode=mode.value,
        mode_dir=mode_dir,
        command=command,
        args=rendered_args,
        default_transcript=default_transcript,
        default_chapters=default_chapters,
        episode_yaml=episode_yaml,
    )

    typer.echo(f"Workspace: {workspace}")
    typer.echo(f"Transcript ({mode.value}): {mode_transcript}")


def _validate_command(command: str) -> str:
    value = command.strip()
    if not value:
        raise typer.BadParameter("transcribe command must be non-empty")
    if any(ch.isspace() for ch in value):
        raise typer.BadParameter("transcribe command must not contain whitespace; use --arg for extra flags")
    return value


def _render_args(
    args: tuple[str, ...],
    *,
    mode: str,
    output_dir: Path,
    workspace: Path,
) -> list[str]:
    rendered: list[str] = []
    mapping = {
        "mode": mode,
        "output_dir": str(output_dir),
        "workspace": str(workspace),
    }
    for raw in args:
        try:
            rendered.append(raw.format(**mapping))
        except KeyError as exc:
            raise typer.BadParameter(f"Unknown placeholder in transcribe args: {raw}") from exc
    return rendered


def _run_transcriber(
    *,
    command: str,
    args: list[str],
    cwd: Path,
    timeout_seconds: float | None,
) -> None:
    try:
        result = subprocess.run(
            [command, *args],
            text=True,
            capture_output=True,
            check=False,
            cwd=str(cwd),
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise typer.BadParameter(f"Transcribe command not found: {command}") from exc
    except subprocess.TimeoutExpired as exc:
        raise typer.BadParameter(f"Transcribe command timed out after {exc.timeout} seconds.") from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        if detail:
            detail = f" {detail}"
        raise typer.BadParameter(f"Transcribe command failed with exit code {result.returncode}.{detail}")


def _update_episode_inputs(
    *,
    store: EpisodeWorkspaceStore,
    episode_yaml: dict[str, Any],
    mode: str,
    mode_transcript: Path,
    mode_chapters: Path | None,
    default_transcript: Path,
    default_chapters: Path | None,
) -> None:
    inputs = episode_yaml.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
    inputs = dict(inputs)

    inputs[f"transcript_{mode}"] = _relpath(mode_transcript, store.layout.root)
    inputs["transcript"] = _relpath(default_transcript, store.layout.root)

    if mode_chapters is not None:
        inputs[f"chapters_{mode}"] = _relpath(mode_chapters, store.layout.root)
        if default_chapters is not None:
            inputs["chapters"] = _relpath(default_chapters, store.layout.root)

    episode_yaml["inputs"] = inputs
    store.write_episode_yaml(episode_yaml)


def _write_transcribe_provenance(
    *,
    store: EpisodeWorkspaceStore,
    mode: str,
    mode_dir: Path,
    command: str,
    args: list[str],
    default_transcript: Path,
    default_chapters: Path | None,
    episode_yaml: dict[str, Any],
) -> None:
    outputs: dict[str, str] = {
        "mode_dir": _relpath(mode_dir, store.layout.root),
        "mode_transcript": _relpath(mode_dir / "transcript.txt", store.layout.root),
        "default_transcript": _relpath(default_transcript, store.layout.root),
    }
    mode_chapters = mode_dir / "chapters.txt"
    if mode_chapters.exists():
        outputs["mode_chapters"] = _relpath(mode_chapters, store.layout.root)
    if default_chapters is not None:
        outputs["default_chapters"] = _relpath(default_chapters, store.layout.root)

    payload: dict[str, object] = {
        "version": 1,
        "mode": mode,
        "command": command,
        "args": list(args),
        "created_at": datetime.now(UTC).isoformat(),
        "outputs": outputs,
    }
    episode_id = episode_yaml.get("episode_id")
    if isinstance(episode_id, str) and episode_id.strip():
        payload["episode_id"] = episode_id

    sources = episode_yaml.get("sources")
    if isinstance(sources, dict):
        payload["sources"] = sources
    tracks = episode_yaml.get("tracks")
    if isinstance(tracks, list):
        payload["tracks"] = tracks

    provenance_path = mode_dir / "provenance.json"
    atomic_write_text(provenance_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _relpath(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()
