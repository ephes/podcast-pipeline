from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer

from podcast_pipeline.auphonic_api import AuphonicApiError, AuphonicClient, load_auphonic_credentials
from podcast_pipeline.auphonic_payload import AuphonicConfigError, build_auphonic_payload
from podcast_pipeline.domain.models import EpisodeWorkspace
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def run_produce(*, workspace: Path, dry_run: bool) -> None:
    store = EpisodeWorkspaceStore(workspace)
    if not store.layout.episode_yaml.exists():
        raise typer.BadParameter(f"Missing episode.yaml in {workspace}")
    episode_yaml = store.read_episode_yaml()
    try:
        payload = build_auphonic_payload(episode_yaml=episode_yaml, workspace=workspace)
    except AuphonicConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if dry_run:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    try:
        credentials = load_auphonic_credentials()
        # Single flight per episode: hold an exclusive lock for the whole
        # start/wait/download cycle and read the stored production UUID only
        # after acquiring it, so a concurrent run cannot start a second paid
        # production for the same workspace.
        with _produce_lock(store), AuphonicClient(credentials) as client:
            workspace_state = _load_workspace_state(store, episode_yaml)
            production_uuid = workspace_state.auphonic_production_uuid
            if production_uuid is None:
                production = client.start_production(payload)
                production_uuid = production.uuid
                # Re-read state.json under its lock so selections written while
                # this run was starting are kept.
                store.set_auphonic_production_uuid(production_uuid, default=workspace_state)

            production = client.wait_for_production(
                production_uuid,
                poll_interval=15.0,
                timeout_seconds=60 * 60,
            )
            output_files = production.output_files
            if not output_files:
                output_files = client.list_output_files(production_uuid)
            if not output_files:
                raise AuphonicApiError("Auphonic production completed without output files.")
            client.download_outputs(output_files, store.layout.auphonic_outputs_dir)
    except AuphonicApiError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Auphonic production complete: {production_uuid}")
    typer.echo(f"Auphonic outputs: {store.layout.auphonic_outputs_dir}")


class ProduceInProgressError(AuphonicApiError):
    """Another ``produce`` run holds the workspace's production lock."""


@contextmanager
def _produce_lock(store: EpisodeWorkspaceStore) -> Iterator[None]:
    lock_path = store.layout.auphonic_dir / ".produce.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProduceInProgressError(
                f"Another Auphonic production is already running for {store.layout.root}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_workspace_state(store: EpisodeWorkspaceStore, episode_yaml: dict[str, object]) -> EpisodeWorkspace:
    if store.layout.state_json.exists():
        return store.read_state()
    episode_id = episode_yaml.get("episode_id")
    if isinstance(episode_id, str) and episode_id.strip():
        return EpisodeWorkspace(episode_id=episode_id.strip(), root_dir=".")
    return EpisodeWorkspace(episode_id=store.layout.root.name, root_dir=".")
