from __future__ import annotations

import os
import re
import shutil
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


def _empty_directory(fd: int) -> None:
    """Delete the contents of the directory ``fd`` refers to, never following its pathname."""
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise OSError("descriptor-bound cleanup is not supported on this platform")
    for name in os.listdir(fd):
        if stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
            shutil.rmtree(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)


def _discard_failed_init(*, root: Path, staging: Path | None, staging_fd: int | None) -> str:
    """Empty this invocation's staging directory and release its empty claim on ``root``.

    Content is only deleted through ``staging_fd``. Pathnames are only passed to
    rmdir, which cannot remove a populated directory that replaced ours.
    """
    notes = ""
    if staging is not None:
        try:
            if staging_fd is not None:
                _empty_directory(staging_fd)
            os.rmdir(staging)
        except FileNotFoundError:
            pass
        except OSError as cleanup_exc:
            notes += f"; staging directory left in place: {staging}: {cleanup_exc}"
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

    # The exclusive mkdir above only claims the name. Everything is built in a
    # private sibling directory and moved over the still-empty claim in one
    # rename, so a failure never requires a recursive delete at ``root``: the
    # claim is released with rmdir, which cannot remove a populated replacement.
    # The staging directory is identified by an open descriptor; its contents
    # are only ever deleted through that descriptor.
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
    except Exception as exc:
        notes = _discard_failed_init(root=root, staging=staging, staging_fd=staging_fd)
        raise typer.BadParameter(f"failed to initialize workspace: {root}: {exc}{notes}") from exc
    finally:
        if staging_fd is not None:
            os.close(staging_fd)

    typer.echo(f"Workspace: {root}")
