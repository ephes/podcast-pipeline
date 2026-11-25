from __future__ import annotations

import fcntl
import json
import stat
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from podcast_pipeline.auphonic_api import (
    AuphonicApiError,
    AuphonicClient,
    AuphonicNotFoundError,
    AuphonicProduction,
    load_auphonic_credentials,
    production_state,
    update_requests,
)
from podcast_pipeline.auphonic_history import (
    CleanupPlan,
    describe_record,
    mark_deleted,
    mark_outputs_downloaded,
    mark_rerun_started,
    plan_cleanup,
    update_record,
)
from podcast_pipeline.auphonic_payload import (
    AuphonicConfigError,
    build_auphonic_payload,
    resolve_cleanup_superseded,
)
from podcast_pipeline.domain.models import EpisodeWorkspace
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore, WorkspaceStoreError

RESTART_REASON = "replaced by `podcast produce --restart`"


def run_produce(
    *,
    workspace: Path,
    dry_run: bool,
    restart: bool = False,
    update: bool = False,
    cleanup: bool | None = None,
) -> None:
    """Run the Auphonic production; ``cleanup`` None follows ``auphonic.cleanup_superseded``."""
    if restart and update:
        raise typer.BadParameter("--restart and --update cannot be combined.")
    store = EpisodeWorkspaceStore(workspace)
    if not store.layout.episode_yaml.exists():
        raise typer.BadParameter(f"Missing episode.yaml in {workspace}")
    try:
        # Real production owns its request snapshot and complete paid-operation cycle.
        # Dry runs remain read-only and never acquire production ownership.
        with nullcontext() if dry_run else _produce_lock(store):
            episode_yaml = store.read_episode_yaml()
            try:
                payload = build_auphonic_payload(episode_yaml=episode_yaml, workspace=workspace)
                if cleanup is None:
                    cleanup = resolve_cleanup_superseded(episode_yaml)
            except AuphonicConfigError as exc:
                raise typer.BadParameter(str(exc)) from exc

            if dry_run:
                if update:
                    payload = _describe_update(_stored_production_uuid(store, episode_yaml), payload)
                typer.echo(json.dumps(payload, indent=2, sort_keys=True))
                return

            credentials = load_auphonic_credentials()
            with AuphonicClient(credentials) as client:
                production = _produce(client, store, episode_yaml, payload, restart=restart, update=update)
                typer.echo(f"Auphonic production complete: {production.uuid}")
                typer.echo(f"Auphonic outputs: {store.layout.auphonic_outputs_dir}")
                if cleanup:
                    _cleanup_after_produce(client, store)
    except AuphonicApiError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _produce(
    client: AuphonicClient,
    store: EpisodeWorkspaceStore,
    episode_yaml: dict[str, object],
    payload: dict[str, Any],
    *,
    restart: bool,
    update: bool,
) -> AuphonicProduction:
    workspace_state = _load_workspace_state(store, episode_yaml)
    production_uuid = workspace_state.auphonic_production_uuid
    not_started_grace = 0.0
    rerun_grace = 0.0
    superseded_reason: str | None = None
    if restart and production_uuid is not None:
        previous_state = _observed_state(client, production_uuid)
        typer.echo(
            f"Discarding stored Auphonic production {production_uuid} (status: {previous_state or 'unknown'}); "
            "starting a new one."
        )
        discarded = production_uuid
        store.update_auphonic_productions(
            lambda state: update_record(state, discarded, last_known_state=previous_state),
            default=workspace_state,
        )
        superseded_reason = RESTART_REASON
        production_uuid = None
    if update:
        production_uuid, rerun_grace = _rerun(client, store, workspace_state, production_uuid, payload)
    elif production_uuid is None:
        production_uuid = _start_new(client, store, workspace_state, payload, superseded_reason=superseded_reason)
        not_started_grace = 120.0
    else:
        _check_resumable(_fetch_stored(client, store, workspace_state, production_uuid), payload)

    try:
        production = client.wait_for_production(
            production_uuid,
            poll_interval=15.0,
            timeout_seconds=60 * 60,
            not_started_grace_seconds=not_started_grace,
            rerun_grace_seconds=rerun_grace,
        )
    except AuphonicNotFoundError as exc:
        raise _missing_production(store, workspace_state, production_uuid) from exc
    output_files = production.output_files
    if not output_files:
        output_files = client.list_output_files(production_uuid)
    if not output_files:
        raise AuphonicApiError("Auphonic production completed without output files.")
    paths = client.download_outputs(output_files, store.layout.auphonic_outputs_dir)
    receipt = {path.relative_to(store.layout.root).as_posix(): _local_output_size(store, path) for path in paths}
    finished = production_uuid
    store.update_auphonic_productions(
        lambda state: mark_outputs_downloaded(state, finished, receipt),
        default=workspace_state,
    )
    return production


def _start_new(
    client: AuphonicClient,
    store: EpisodeWorkspaceStore,
    workspace_state: EpisodeWorkspace,
    payload: dict[str, Any],
    *,
    superseded_reason: str | None,
) -> str:
    def remember(created_uuid: str) -> None:
        # Store the UUID as soon as the production exists, before the
        # paid start request: if the start's outcome is unknown (a
        # timeout), a rerun follows this production instead of paying
        # for another. Re-read state.json under its lock so selections
        # written meanwhile are kept. The previous production stays in
        # the history as superseded, so it can be cleaned up later.
        store.set_auphonic_production_uuid(created_uuid, default=workspace_state, superseded_reason=superseded_reason)

    typer.echo(_describe_inputs(payload))
    return client.start_production(payload, on_created=remember).uuid


def _rerun(
    client: AuphonicClient,
    store: EpisodeWorkspaceStore,
    workspace_state: EpisodeWorkspace,
    production_uuid: str | None,
    payload: dict[str, Any],
) -> tuple[str, float]:
    """Update the stored production and start it again; return its UUID and the stale-status grace period."""
    production_uuid = _stored_production_uuid_or_fail(production_uuid)
    stored = _fetch_stored(client, store, workspace_state, production_uuid)
    typer.echo(
        f"Updating Auphonic production {production_uuid} from episode.yaml and starting it again "
        "(the uploaded inputs are reused)."
    )
    if production_state(stored) in {"done", "changed"}:
        typer.echo("Note: re-running a finished production costs Auphonic credits again.")
    rerun_uuid = production_uuid

    def forget_downloaded_outputs() -> None:
        # The downloaded outputs belong to the previous run from now on, so no
        # cleanup runs until this run's outputs are downloaded.
        store.update_auphonic_productions(lambda state: mark_rerun_started(state, rerun_uuid), default=workspace_state)

    started = client.rerun_production(stored, payload, before_update=forget_downloaded_outputs)
    # Until Auphonic reports the production running, its previous Done status is stale.
    return production_uuid, 0.0 if production_state(started) == "running" else 120.0


class StoredProductionMissingError(AuphonicApiError):
    """The production stored in state.json no longer exists in Auphonic (404)."""


def _fetch_stored(
    client: AuphonicClient,
    store: EpisodeWorkspaceStore,
    workspace_state: EpisodeWorkspace,
    production_uuid: str,
) -> AuphonicProduction:
    try:
        return client.fetch_production(production_uuid)
    except AuphonicNotFoundError as exc:
        raise _missing_production(store, workspace_state, production_uuid) from exc


def _missing_production(
    store: EpisodeWorkspaceStore,
    workspace_state: EpisodeWorkspace,
    production_uuid: str,
) -> StoredProductionMissingError:
    """Record that the stored production is gone and explain the way out; never start a new one silently."""
    store.update_auphonic_productions(
        lambda state: update_record(state, production_uuid, last_known_state="missing"),
        default=workspace_state,
    )
    return StoredProductionMissingError(_missing_message(production_uuid))


def _missing_message(production_uuid: str) -> str:
    return (
        f"Auphonic production {production_uuid} stored in state.json no longer exists in Auphonic (it was deleted, "
        "for example in the Auphonic web UI). Its uploaded stems were deleted with it, so it can neither be resumed "
        "nor re-run with --update. Run `podcast produce --restart` to start a new (paid) production."
    )


def _observed_state(client: AuphonicClient, production_uuid: str) -> str | None:
    """The production's state for the history, or None when Auphonic cannot be asked."""
    try:
        return production_state(client.fetch_production(production_uuid))
    except AuphonicNotFoundError:
        return "missing"
    except AuphonicApiError:
        return None


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


# --- cleanup of superseded productions -----------------------------------------


class AuphonicCleanupBlockedError(AuphonicApiError):
    """Superseded productions are kept: the current one is not Done with downloaded outputs."""


@dataclass(frozen=True)
class CleanupResult:
    deleted: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    settlement_pending: tuple[str, ...] = ()


def run_auphonic_cleanup(*, workspace: Path, dry_run: bool) -> None:
    """List (``dry_run``) or delete the superseded Auphonic productions recorded in state.json."""
    store = EpisodeWorkspaceStore(workspace)
    if not store.layout.state_json.exists():
        typer.echo(f"No state.json in {workspace}, so no Auphonic productions are recorded.")
        return
    try:
        plan = plan_cleanup(store.read_state())
    except WorkspaceStoreError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if dry_run:
        _print_plan(plan, store)
        return
    try:
        credentials = load_auphonic_credentials()
        # The produce lock keeps a concurrent `podcast produce` from changing
        # the current production while superseded ones are deleted.
        with _produce_lock(store), AuphonicClient(credentials) as client:
            result = cleanup_superseded(client, store)
    except (AuphonicApiError, WorkspaceStoreError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if problems := _cleanup_problems(result):
        raise typer.BadParameter(" ".join(problems))


def _local_output_size(store: EpisodeWorkspaceStore, path: Path) -> int:
    layout = store.layout
    try:
        # Lexical check only: `auphonic/` or `auphonic/outputs/` may be a
        # symlink (for example to an external media disk), as for downloads.
        if path.parent != layout.auphonic_outputs_dir:
            raise ValueError("path is outside the workspace output directory")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("path is not a regular file")
        return info.st_size
    except (OSError, ValueError) as exc:
        raise AuphonicApiError(f"Cannot verify local Auphonic output {path}: {exc}") from exc


def _local_output_problem(store: EpisodeWorkspaceStore, receipt: dict[str, int]) -> str | None:
    for relative, size in receipt.items():
        if Path(relative).is_absolute():
            return "the output receipt contains an absolute path; run `podcast produce` to refresh the outputs"
        path = store.layout.root / relative
        try:
            actual_size = _local_output_size(store, path)
        except AuphonicApiError as exc:
            return f"{exc}; run `podcast produce` to refresh the outputs"
        if actual_size != size:
            return f"local Auphonic output {path} changed size; run `podcast produce` to refresh the outputs"
    return None


def _print_plan(plan: CleanupPlan, store: EpisodeWorkspaceStore) -> None:
    if not plan.candidates:
        typer.echo("No superseded Auphonic productions to delete.")
        return
    current = plan.current.uuid if plan.current is not None else "none"
    typer.echo(
        f"Dry run: would delete {len(plan.candidates)} superseded Auphonic production(s) of this workspace "
        f"(current production: {current}, kept):"
    )
    for record in plan.candidates:
        typer.echo(f"  {describe_record(record)}")
    blocked = plan.blocked_reason
    if blocked is None and plan.current is not None:
        blocked = _local_output_problem(store, plan.current.output_receipt)
    if blocked is not None:
        typer.echo(f"Nothing would be deleted right now: {blocked}.")
    else:
        typer.echo(
            "Run `podcast auphonic-cleanup --delete` to delete them; deletion cannot be undone. "
            "Auphonic is asked first that the current production is Done."
        )


def cleanup_superseded(
    client: AuphonicClient,
    store: EpisodeWorkspaceStore,
) -> CleanupResult:
    """Delete this workspace's superseded productions from the Auphonic account.

    Only productions in the state.json history with status ``superseded`` are
    deleted, never the current one, and only when the current production is
    Done (asked from Auphonic right before deleting, even after a produce run
    just polled it: it may have changed or been deleted while the outputs downloaded)
    and the local outputs of its latest run match the receipt. A superseded production
    that is still running is skipped. Call it while holding the produce lock.
    """
    state = store.read_state()
    plan = plan_cleanup(state)
    if not plan.candidates:
        typer.echo("No superseded Auphonic productions to delete.")
        return CleanupResult()
    if plan.current is None or plan.blocked_reason is not None:
        raise AuphonicCleanupBlockedError(f"Not deleting superseded Auphonic productions: {plan.blocked_reason}.")
    try:
        current = client.fetch_production(plan.current.uuid)
    except AuphonicNotFoundError as exc:
        current_uuid = plan.current.uuid
        store.update_auphonic_productions(
            lambda s: update_record(s, current_uuid, last_known_state="missing"), default=state
        )
        raise AuphonicCleanupBlockedError(
            f"Not deleting superseded Auphonic productions: {_missing_message(current_uuid)}"
        ) from exc
    if production_state(current) != "done":
        raise AuphonicCleanupBlockedError(
            f"Not deleting superseded Auphonic productions: the current production {current.uuid} is not Done "
            f"(status: {current.status_string or current.status})."
        )

    if problem := _local_output_problem(store, plan.current.output_receipt):
        raise AuphonicCleanupBlockedError(f"Not deleting superseded Auphonic productions: {problem}.")

    deleted: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    settlement_pending: list[str] = []
    outcomes = {"deleted": deleted, "skipped": skipped, "failed": failed, "settlement_pending": settlement_pending}
    for record in plan.candidates:
        outcome = _delete_superseded(client, store, state, record.uuid, describe_record(record))
        outcomes[outcome].append(record.uuid)
        if outcome == "settlement_pending":
            break
    return CleanupResult(
        deleted=tuple(deleted),
        skipped=tuple(skipped),
        failed=tuple(failed),
        settlement_pending=tuple(settlement_pending),
    )


def _delete_superseded(
    client: AuphonicClient,
    store: EpisodeWorkspaceStore,
    state: EpisodeWorkspace,
    uuid: str,
    description: str,
) -> str:
    try:
        observed = production_state(client.fetch_production(uuid))
    except AuphonicNotFoundError:
        return _settle_deleted(store, state, uuid, observed="missing")
    except AuphonicApiError as exc:
        typer.echo(f"Could not check superseded Auphonic production {uuid}: {exc}", err=True)
        return "failed"
    if observed == "running":
        store.update_auphonic_productions(lambda s: update_record(s, uuid, last_known_state=observed), default=state)
        typer.echo(f"Skipping superseded Auphonic production {uuid}: it is still running.")
        return "skipped"
    typer.echo(f"Deleting superseded Auphonic production {description}")
    try:
        client.delete_production(uuid)
    except AuphonicNotFoundError:
        pass
    except AuphonicApiError as exc:
        typer.echo(f"Could not delete Auphonic production {uuid}: {exc}", err=True)
        return "failed"
    return _settle_deleted(store, state, uuid, observed=observed)


def _settle_deleted(store: EpisodeWorkspaceStore, state: EpisodeWorkspace, uuid: str, *, observed: str) -> str:
    try:
        store.update_auphonic_productions(lambda s: mark_deleted(s, uuid, last_known_state=observed), default=state)
    except (WorkspaceStoreError, OSError) as exc:
        typer.echo(f"Production {uuid} is gone from Auphonic. Could not record it in state.json ({exc}).", err=True)
        return "settlement_pending"
    if observed == "missing":
        typer.echo(f"Superseded Auphonic production {uuid} no longer exists and was recorded as deleted.")
    else:
        typer.echo(f"Deleted Auphonic production {uuid}.")
    return "deleted"


def _cleanup_problems(result: CleanupResult) -> list[str]:
    problems = []
    if result.failed:
        problems.append(
            f"Could not delete the superseded Auphonic productions {', '.join(result.failed)}. "
            "Retry with `podcast auphonic-cleanup --delete`."
        )
    if result.settlement_pending:
        problems.append(
            f"Auphonic productions {', '.join(result.settlement_pending)} are gone remotely and still need "
            "to be recorded as deleted in state.json. Fix the workspace write error and retry with "
            "`podcast auphonic-cleanup --delete`."
        )
    return problems


def _cleanup_after_produce(client: AuphonicClient, store: EpisodeWorkspaceStore) -> None:
    """Opt-in cleanup after a successful produce; problems are warnings, the production itself succeeded."""
    try:
        result = cleanup_superseded(client, store)
    except (AuphonicApiError, WorkspaceStoreError, OSError) as exc:
        typer.echo(f"Warning: Auphonic cleanup failed: {exc}", err=True)
        return
    for problem in _cleanup_problems(result):
        typer.echo(f"Warning: {problem}", err=True)
