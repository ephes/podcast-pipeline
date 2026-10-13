from __future__ import annotations

import os
import re
import stat
import tempfile
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


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_owned_staging(staging: Path) -> int:
    """Open the freshly created staging directory and confirm it is still ours.

    The descriptor, not the pathname, identifies the directory from here on.
    """
    fd = os.open(staging, _DIRECTORY_FLAGS)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700 or os.listdir(fd):
            raise OSError(f"staging directory was replaced: {staging}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release_failed_init(*, root: Path, staging: Path | None) -> str:
    """Release this invocation's empty claim on ``root`` after a failure.

    Nothing is deleted: the claim is released with rmdir, which cannot remove
    content, and the staging directory is left in place and reported, because
    no check can prove that a directory found under its name is still ours.
    """
    notes = ""
    if staging is not None:
        notes += f"; partial workspace left in staging directory: {staging}"
    try:
        os.rmdir(root)
    except FileNotFoundError:
        pass
    except OSError:
        notes += "; existing workspace content preserved"
    return notes


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

    # Guarantees: no partial workspace is left at ``root``, so a retry works;
    # an existing workspace and content that appears at ``root`` while init runs
    # are preserved; nothing is deleted on failure or interruption.
    # Outside the threat model: another process running as the same user with
    # write access to the parent directory replacing the claim or the staging
    # directory while init runs. Such a process can delete or overwrite the
    # workspace directly, so the checks below are hardening, not a boundary.
    #
    # The exclusive mkdir above only claims the name. Everything is built in a
    # private sibling directory and moved over the still-empty claim in one
    # rename. A failure deletes nothing: the claim is released with rmdir, which
    # cannot remove a populated replacement, and the staging directory stays.
    # The open descriptor identifies the staging directory for the mode change
    # and for the check that its name still refers to it before publication.
    staging: Path | None = None
    staging_fd: int | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=".podcast-init-", dir=root.parent))
        staging_fd = _open_owned_staging(staging)
        _create_workspace_dirs(EpisodeWorkspaceLayout(root=staging))

        store = EpisodeWorkspaceStore(staging)
        store.write_episode_yaml({"episode_id": episode_id, "inputs": {}})
        store.write_state(EpisodeWorkspace(episode_id=episode_id, root_dir="."))

        claimed = root.lstat()
        if stat.S_ISDIR(claimed.st_mode):
            os.fchmod(staging_fd, stat.S_IMODE(claimed.st_mode))
        owned, current = os.fstat(staging_fd), staging.lstat()
        if (owned.st_dev, owned.st_ino) != (current.st_dev, current.st_ino):
            raise OSError(f"staging directory was replaced: {staging}")
        os.rename(staging, root)
    except BaseException as exc:
        notes = _release_failed_init(root=root, staging=staging)
        if not isinstance(exc, Exception):
            # Ctrl-C and similar: release the claim, then keep interrupt semantics.
            typer.echo(f"initialization interrupted: {root}{notes}", err=True)
            raise
        raise typer.BadParameter(f"failed to initialize workspace: {root}: {exc}{notes}") from exc
    finally:
        if staging_fd is not None:
            os.close(staging_fd)

    typer.echo(f"Workspace: {root}")
