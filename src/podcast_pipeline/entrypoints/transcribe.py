from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from string import Formatter
from typing import Any

import typer

from podcast_pipeline.workspace_store import EpisodeWorkspaceStore, WorkspaceStoreError, atomic_write_text


class TranscriptionMode(StrEnum):
    draft = "draft"
    final = "final"


_LEGACY_DEFAULT_TRANSCRIBE_ARGS: tuple[str, ...] = ("--mode", "{mode}", "--output-dir", "{output_dir}")
_PODCAST_TRANSCRIPT_DEFAULT_ARGS: tuple[str, ...] = ("{audio_file}",)
_PODCAST_TRANSCRIPT_COMMAND_NAMES = {"transcribe", "podcast-transcript"}
_PREFERRED_AUDIO_TRACK_ROLES = {"mix", "master", "final", "mixdown"}


@dataclass(frozen=True)
class TranscribeConfig:
    command: str = "transcribe"
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

    _args, audio_file, rendered_args = _prepare_transcriber_invocation(
        command=command,
        config=config,
        episode_yaml=episode_yaml,
        mode=mode.value,
        mode_dir=mode_dir,
        workspace=workspace,
    )
    _run_transcriber(
        command=command,
        args=rendered_args,
        cwd=workspace,
        timeout_seconds=config.timeout_seconds,
    )

    mode_transcript = mode_dir / "transcript.txt"
    if not mode_transcript.exists() and audio_file is not None:
        _import_podcast_transcript_output(
            command=command,
            args=rendered_args,
            audio_file=audio_file,
            transcript_path=mode_transcript,
        )
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


def _default_args_for_command(command: str) -> tuple[str, ...]:
    if Path(command).name in _PODCAST_TRANSCRIPT_COMMAND_NAMES:
        return _PODCAST_TRANSCRIPT_DEFAULT_ARGS
    return _LEGACY_DEFAULT_TRANSCRIBE_ARGS


def _prepare_transcriber_invocation(
    *,
    command: str,
    config: TranscribeConfig,
    episode_yaml: dict[str, Any],
    mode: str,
    mode_dir: Path,
    workspace: Path,
) -> tuple[tuple[str, ...], Path | None, list[str]]:
    if config.args is None:
        args = _default_args_for_command(command)
    else:
        args = _default_args_for_command(command) + config.args

    audio_file: Path | None = None
    if _args_need_audio_file(args):
        audio_file = _resolve_audio_file(episode_yaml=episode_yaml, workspace=workspace)

    rendered_args = _render_args(
        args,
        mode=mode,
        output_dir=mode_dir,
        workspace=workspace,
        audio_file=audio_file,
    )
    return args, audio_file, rendered_args


def _args_need_audio_file(args: tuple[str, ...]) -> bool:
    formatter = Formatter()
    for raw in args:
        for _literal, field_name, _format_spec, _conversion in formatter.parse(raw):
            if field_name == "audio_file":
                return True
    return False


def _render_args(
    args: tuple[str, ...],
    *,
    mode: str,
    output_dir: Path,
    workspace: Path,
    audio_file: Path | None,
) -> list[str]:
    rendered: list[str] = []
    mapping = {
        "mode": mode,
        "output_dir": str(output_dir),
        "workspace": str(workspace),
    }
    if audio_file is not None:
        mapping["audio_file"] = str(audio_file)
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


def _resolve_audio_file(
    *,
    episode_yaml: dict[str, Any],
    workspace: Path,
) -> Path:
    from_auphonic = _resolve_audio_file_from_auphonic(episode_yaml=episode_yaml, workspace=workspace)
    if from_auphonic is not None:
        return from_auphonic

    from_tracks = _resolve_audio_file_from_tracks(episode_yaml=episode_yaml, workspace=workspace)
    if from_tracks is not None:
        return from_tracks

    raise typer.BadParameter(
        "Could not determine a single source audio file. Set auphonic.input_file in episode.yaml "
        "or provide one preferred mix/master/final track."
    )


def _resolve_audio_file_from_auphonic(
    *,
    episode_yaml: dict[str, Any],
    workspace: Path,
) -> Path | None:
    raw = episode_yaml.get("auphonic")
    if not isinstance(raw, dict):
        return None

    input_file = raw.get("input_file")
    if input_file is not None:
        return _resolve_declared_audio_path(
            input_file,
            source="auphonic.input_file",
            workspace=workspace,
        )

    input_files = raw.get("input_files")
    if input_files is None:
        return None
    if not isinstance(input_files, list) or not input_files:
        raise typer.BadParameter("auphonic.input_files must be a non-empty list of strings")
    if len(input_files) != 1:
        raise typer.BadParameter(
            "podcast transcribe needs a single source audio file; set auphonic.input_file "
            "or reduce auphonic.input_files to one entry"
        )
    return _resolve_declared_audio_path(
        input_files[0],
        source="auphonic.input_files[0]",
        workspace=workspace,
    )


def _resolve_declared_audio_path(
    value: object,
    *,
    source: str,
    workspace: Path,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise typer.BadParameter(f"{source} must be a non-empty string")
    resolved = Path(value).expanduser()
    if not resolved.is_absolute():
        resolved = (workspace / resolved).resolve()
    else:
        resolved = resolved.resolve()
    if not resolved.exists():
        raise typer.BadParameter(f"Configured audio file does not exist: {resolved}")
    if resolved.is_dir():
        raise typer.BadParameter(f"Configured audio file is a directory: {resolved}")
    return resolved


def _resolve_audio_file_from_tracks(
    *,
    episode_yaml: dict[str, Any],
    workspace: Path,
) -> Path | None:
    tracks = episode_yaml.get("tracks")
    if not isinstance(tracks, list):
        return None

    preferred, fallback = _collect_track_audio_candidates(
        tracks=tracks,
        base_dir=_resolve_reaper_media_dir(episode_yaml),
        workspace=workspace,
    )

    if len(preferred) == 1:
        return preferred[0]
    if len(preferred) > 1:
        raise typer.BadParameter(
            "Found multiple preferred mix/master/final tracks. Set auphonic.input_file to choose one."
        )
    if len(fallback) == 1:
        return fallback[0]
    if len(fallback) > 1:
        raise typer.BadParameter(
            "Found multiple track audio files and no single preferred mix track. "
            "Set auphonic.input_file to choose one."
        )
    return None


def _collect_track_audio_candidates(
    *,
    tracks: list[Any],
    base_dir: Path | None,
    workspace: Path,
) -> tuple[list[Path], list[Path]]:
    preferred: list[Path] = []
    fallback: list[Path] = []
    for item in tracks:
        resolved = _resolve_track_candidate(item=item, base_dir=base_dir, workspace=workspace)
        if resolved is None:
            continue
        role, path = resolved
        if role in _PREFERRED_AUDIO_TRACK_ROLES:
            preferred.append(path)
        else:
            fallback.append(path)
    return preferred, fallback


def _resolve_track_candidate(
    *,
    item: Any,
    base_dir: Path | None,
    workspace: Path,
) -> tuple[str | None, Path] | None:
    if not isinstance(item, dict):
        return None
    path_raw = item.get("path")
    if not isinstance(path_raw, str) or not path_raw.strip():
        return None

    resolved = _resolve_track_audio_path(path_raw, base_dir=base_dir, workspace=workspace)
    if not resolved.exists():
        raise typer.BadParameter(f"Track audio file does not exist: {resolved}")
    if resolved.is_dir():
        raise typer.BadParameter(f"Track audio path is a directory: {resolved}")

    role = item.get("role")
    if isinstance(role, str):
        return role.strip().lower(), resolved
    return None, resolved


def _resolve_reaper_media_dir(episode_yaml: dict[str, Any]) -> Path | None:
    sources = episode_yaml.get("sources")
    if not isinstance(sources, dict):
        return None
    raw = sources.get("reaper_media_dir")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw).expanduser()


def _resolve_track_audio_path(raw: str, *, base_dir: Path | None, workspace: Path) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    if base_dir is not None:
        return (base_dir / path).resolve()
    return (workspace / path).resolve()


def _import_podcast_transcript_output(
    *,
    command: str,
    args: list[str],
    audio_file: Path,
    transcript_path: Path,
) -> None:
    if not _uses_podcast_transcript(command=command, args=args):
        return
    source = _podcast_transcript_plaintext_path(audio_file)
    if not source.exists():
        return
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, transcript_path)


def _uses_podcast_transcript(*, command: str, args: list[str]) -> bool:
    del args
    return Path(command).name in _PODCAST_TRANSCRIPT_COMMAND_NAMES


def _podcast_transcript_plaintext_path(audio_file: Path) -> Path:
    transcript_dir = os.environ.get("TRANSCRIPT_DIR")
    if transcript_dir:
        base_dir = Path(transcript_dir).expanduser()
    else:
        transcript_home = Path(os.environ.get("TRANSCRIPT_HOME", "~/.podcast-transcripts")).expanduser()
        base_dir = transcript_home / "transcripts"
    prefix = audio_file.stem
    return base_dir / prefix / f"{prefix}.txt"


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
