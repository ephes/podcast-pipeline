from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import typer

from podcast_pipeline.domain.models import EpisodeWorkspace
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore, episode_workspace_dir

_EPISODE_ID_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def _validate_episode_id(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise typer.BadParameter("episode_id must be non-empty")
    if "/" in candidate or "\\" in candidate:
        raise typer.BadParameter("episode_id must not contain path separators")
    if not _EPISODE_ID_RE.fullmatch(candidate):
        raise typer.BadParameter("episode_id must contain only letters, digits, '.', '_', or '-'")
    if candidate != candidate.strip("._-"):
        raise typer.BadParameter("episode_id must not start or end with '.', '_', or '-'")
    return candidate


def _create_workspace_dirs(layout: EpisodeWorkspaceLayout) -> None:
    dirs = (
        layout.transcript_dir,
        layout.transcript_chunks_dir,
        layout.summaries_dir,
        layout.chunk_summaries_dir,
        layout.episode_summary_dir,
        layout.copy_dir,
        layout.copy_candidates_dir,
        layout.copy_reviews_dir,
        layout.copy_selected_dir,
        layout.copy_provenance_dir,
        layout.copy_protocol_dir,
        layout.auphonic_downloads_dir,
        layout.auphonic_outputs_dir,
    )
    for path in dirs:
        path.mkdir(parents=True, exist_ok=True)


def run_init(
    *,
    workspace: Path | None,
    episode_id: str,
    project_root: Path,
) -> None:
    episode_id = _validate_episode_id(episode_id)
    root = workspace if workspace is not None else episode_workspace_dir(project_root, episode_id)
    if root.exists():
        raise typer.BadParameter(f"workspace already exists: {root}")

    try:
        root.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise typer.BadParameter(f"failed to create workspace: {root}") from exc

    try:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise typer.BadParameter(f"failed to initialize workspace: {root}: {exc}; ownership unavailable") from exc
    owned = os.fstat(directory_fd)
    try:
        layout = EpisodeWorkspaceLayout(root=root)
        _create_workspace_dirs(layout)

        store = EpisodeWorkspaceStore(root)
        store.write_episode_yaml({"episode_id": episode_id, "inputs": {}})
        store.write_state(EpisodeWorkspace(episode_id=episode_id, root_dir="."))
    except Exception as exc:
        suffix = ""
        try:
            current = root.lstat()
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                shutil.rmtree(root)
            else:
                suffix = "; replacement workspace preserved"
        except FileNotFoundError:
            pass
        except OSError as cleanup_exc:
            raise typer.BadParameter(
                f"failed to initialize workspace: {root}: {exc}; cleanup failed: {cleanup_exc}"
            ) from exc
        raise typer.BadParameter(f"failed to initialize workspace: {root}: {exc}{suffix}") from exc
    finally:
        os.close(directory_fd)

    typer.echo(f"Workspace: {root}")
