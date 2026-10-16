from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer

from podcast_pipeline.auphonic_api import (
    AuphonicApiError,
    AuphonicClient,
    AuphonicProduction,
    load_auphonic_credentials,
    production_state,
    update_requests,
)
from podcast_pipeline.auphonic_payload import AuphonicConfigError, build_auphonic_payload
from podcast_pipeline.domain.models import EpisodeWorkspace
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def run_produce(*, workspace: Path, dry_run: bool, restart: bool = False, update: bool = False) -> None:
    if restart and update:
        raise typer.BadParameter("--restart and --update cannot be combined.")
    store = EpisodeWorkspaceStore(workspace)
    if not store.layout.episode_yaml.exists():
        raise typer.BadParameter(f"Missing episode.yaml in {workspace}")
    episode_yaml = store.read_episode_yaml()
    try:
        payload = build_auphonic_payload(episode_yaml=episode_yaml, workspace=workspace)
    except AuphonicConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if dry_run:
        if update:
            payload = _describe_update(_stored_production_uuid(store, episode_yaml), payload)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    try:
        credentials = load_auphonic_credentials()
        # Single flight per episode: hold an exclusive lock for the whole
        # start/wait/download cycle and read the stored production UUID only
        # after acquiring it, so a concurrent run cannot start a second paid
        # production for the same workspace.
        with _produce_lock(store), AuphonicClient(credentials) as client:
            production_uuid = _produce(client, store, episode_yaml, payload, restart=restart, update=update)
    except AuphonicApiError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Auphonic production complete: {production_uuid}")
    typer.echo(f"Auphonic outputs: {store.layout.auphonic_outputs_dir}")


def _produce(
    client: AuphonicClient,
    store: EpisodeWorkspaceStore,
    episode_yaml: dict[str, object],
    payload: dict[str, Any],
    *,
    restart: bool,
    update: bool,
) -> str:
    workspace_state = _load_workspace_state(store, episode_yaml)
    production_uuid = workspace_state.auphonic_production_uuid
    not_started_grace = 0.0
    rerun_grace = 0.0
    if restart and production_uuid is not None:
        typer.echo(f"Discarding stored Auphonic production {production_uuid}; starting a new one.")
        production_uuid = None
    if update:
        production_uuid, rerun_grace = _rerun(client, production_uuid, payload)
    elif production_uuid is None:
        production_uuid = _start_new(client, store, workspace_state, payload)
        not_started_grace = 120.0
    else:
        _check_resumable(client.fetch_production(production_uuid), payload)

    production = client.wait_for_production(
        production_uuid,
        poll_interval=15.0,
        timeout_seconds=60 * 60,
        not_started_grace_seconds=not_started_grace,
        rerun_grace_seconds=rerun_grace,
    )
    output_files = production.output_files
    if not output_files:
        output_files = client.list_output_files(production_uuid)
    if not output_files:
        raise AuphonicApiError("Auphonic production completed without output files.")
    client.download_outputs(output_files, store.layout.auphonic_outputs_dir)
    return production_uuid


def _start_new(
    client: AuphonicClient,
    store: EpisodeWorkspaceStore,
    workspace_state: EpisodeWorkspace,
    payload: dict[str, Any],
) -> str:
    def remember(created_uuid: str) -> None:
        # Store the UUID as soon as the production exists, before the
        # paid start request: if the start's outcome is unknown (a
        # timeout), a rerun follows this production instead of paying
        # for another. Re-read state.json under its lock so selections
        # written meanwhile are kept.
        store.set_auphonic_production_uuid(created_uuid, default=workspace_state)

    typer.echo(_describe_inputs(payload))
    return client.start_production(payload, on_created=remember).uuid


def _rerun(client: AuphonicClient, production_uuid: str | None, payload: dict[str, Any]) -> tuple[str, float]:
    """Update the stored production and start it again; return its UUID and the stale-status grace period."""
    production_uuid = _stored_production_uuid_or_fail(production_uuid)
    stored = client.fetch_production(production_uuid)
    typer.echo(
        f"Updating Auphonic production {production_uuid} from episode.yaml and starting it again "
        "(the uploaded inputs are reused)."
    )
    if production_state(stored) in {"done", "changed"}:
        typer.echo("Note: re-running a finished production costs Auphonic credits again.")
    started = client.rerun_production(stored, payload)
    # Until Auphonic reports the production running, its previous Done status is stale.
    return production_uuid, 0.0 if production_state(started) == "running" else 120.0


def _describe_update(production_uuid: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        body, track_updates = update_requests(payload)
    except AuphonicApiError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return {"production_uuid": production_uuid, "production_update": body, "track_updates": track_updates}


def _stored_production_uuid(store: EpisodeWorkspaceStore, episode_yaml: dict[str, object]) -> str:
    try:
        return _stored_production_uuid_or_fail(_load_workspace_state(store, episode_yaml).auphonic_production_uuid)
    except AuphonicApiError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _stored_production_uuid_or_fail(production_uuid: str | None) -> str:
    if production_uuid is None:
        raise AuphonicApiError(
            "No Auphonic production is stored in state.json, so there is nothing to update. "
            "Run `podcast produce` without --update to start one."
        )
    return production_uuid


def _describe_inputs(payload: dict[str, Any]) -> str:
    tracks = payload.get("multi_input_files")
    if payload.get("is_multitrack") is True and isinstance(tracks, list):
        ids = ", ".join(str(track.get("id")) for track in tracks if isinstance(track, dict))
        return f"Starting an Auphonic multitrack production with {len(tracks)} tracks: {ids}"
    return f"Starting a single-track Auphonic production from {payload.get('input_file')}"


def _production_kind(multitrack: bool) -> str:
    return "multitrack" if multitrack else "single-track"


def _check_resumable(production: AuphonicProduction, payload: dict[str, Any]) -> None:
    """Refuse to resume a stored production of the other type (for example a pre-mixed single-track one)."""
    expected = payload.get("is_multitrack") is True
    if production.is_multitrack is None or production.is_multitrack == expected:
        return
    raise AuphonicApiError(
        f"Stored Auphonic production {production.uuid} is a {_production_kind(production.is_multitrack)} "
        f"production, but episode.yaml configures a {_production_kind(expected)} production. "
        "Rerun with `podcast produce --restart` to start a new one."
    )


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
