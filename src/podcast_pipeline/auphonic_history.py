"""History of the Auphonic productions an episode workspace created.

``state.json`` keeps every production ``podcast produce`` created for the
workspace in ``auphonic_productions``: the current one (also stored as
``auphonic_production_uuid``) and the ones a later production superseded. Only
superseded entries are ever cleaned up, so nothing outside this history and
never the current production is deleted from the Auphonic account.

The functions here are pure: they take an :class:`EpisodeWorkspace` and return
an updated copy. ``EpisodeWorkspaceStore`` applies them under its state lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from podcast_pipeline.domain.models import (
    AuphonicProductionRecord,
    AuphonicProductionStatus,
    EpisodeWorkspace,
)


def _now() -> datetime:
    return datetime.now(UTC)


def production_history(workspace: EpisodeWorkspace) -> list[AuphonicProductionRecord]:
    """The workspace's productions, including a stored UUID from before the history existed."""
    records = list(workspace.auphonic_productions)
    current = workspace.auphonic_production_uuid
    if current is not None and all(record.uuid != current for record in records):
        records.append(AuphonicProductionRecord(uuid=current, status=AuphonicProductionStatus.current))
    return records


def current_record(workspace: EpisodeWorkspace) -> AuphonicProductionRecord | None:
    for record in production_history(workspace):
        if record.status == AuphonicProductionStatus.current:
            return record
    return None


def record_new_production(workspace: EpisodeWorkspace, uuid: str, *, reason: str | None) -> EpisodeWorkspace:
    """Make ``uuid`` the current production; the previous current one becomes superseded."""
    if uuid == workspace.auphonic_production_uuid:
        return workspace.model_copy(update={"auphonic_productions": production_history(workspace)})
    now = _now()
    records: list[AuphonicProductionRecord] = []
    for record in production_history(workspace):
        if record.uuid == uuid:
            continue
        if record.status == AuphonicProductionStatus.current:
            record = record.model_copy(
                update={
                    "status": AuphonicProductionStatus.superseded,
                    "superseded_at": now,
                    "superseded_by": uuid,
                    "superseded_reason": reason or "replaced by a new production",
                }
            )
        records.append(record)
    records.append(AuphonicProductionRecord(uuid=uuid, created_at=now))
    return workspace.model_copy(update={"auphonic_production_uuid": uuid, "auphonic_productions": records})


def update_record(workspace: EpisodeWorkspace, uuid: str, **changes: object) -> EpisodeWorkspace:
    """Change fields of the history entry for ``uuid``; unknown UUIDs leave the state unchanged."""
    records = production_history(workspace)
    if all(record.uuid != uuid for record in records):
        return workspace
    updated = [record.model_copy(update=changes) if record.uuid == uuid else record for record in records]
    return workspace.model_copy(update={"auphonic_productions": updated})


def mark_outputs_downloaded(workspace: EpisodeWorkspace, uuid: str) -> EpisodeWorkspace:
    return update_record(workspace, uuid, last_known_state="done", outputs_downloaded_at=_now())


def mark_rerun_started(workspace: EpisodeWorkspace, uuid: str) -> EpisodeWorkspace:
    """The production runs again (``--update``): the downloaded outputs no longer match its latest run."""
    return update_record(workspace, uuid, last_known_state=None, outputs_downloaded_at=None)


def mark_deleted(workspace: EpisodeWorkspace, uuid: str, *, last_known_state: str | None = None) -> EpisodeWorkspace:
    record = next((record for record in production_history(workspace) if record.uuid == uuid), None)
    if record is None or record.status != AuphonicProductionStatus.superseded:
        return workspace
    changes: dict[str, object] = {"status": AuphonicProductionStatus.deleted, "deleted_at": _now()}
    if last_known_state is not None:
        changes["last_known_state"] = last_known_state
    return update_record(workspace, uuid, **changes)


@dataclass(frozen=True)
class CleanupPlan:
    """Superseded productions of one workspace that may be deleted from the Auphonic account.

    ``blocked_reason`` is set when the local state already rules out a cleanup:
    there is no current production, or the outputs of its latest run were not
    downloaded. Callers must still confirm with Auphonic that the current
    production is Done before deleting anything.
    """

    current: AuphonicProductionRecord | None
    candidates: tuple[AuphonicProductionRecord, ...]
    blocked_reason: str | None


def plan_cleanup(workspace: EpisodeWorkspace) -> CleanupPlan:
    current = current_record(workspace)
    candidates = tuple(
        record
        for record in production_history(workspace)
        if record.status == AuphonicProductionStatus.superseded
        and record.uuid != workspace.auphonic_production_uuid
        and (current is None or record.uuid != current.uuid)
    )
    blocked: str | None = None
    if current is None:
        blocked = "no current Auphonic production is stored in state.json"
    elif current.last_known_state == "missing":
        blocked = (
            f"the current Auphonic production {current.uuid} no longer exists in Auphonic; "
            "start a new one with `podcast produce --restart`"
        )
    elif current.outputs_downloaded_at is None or current.last_known_state != "done":
        blocked = (
            f"the outputs of the current Auphonic production {current.uuid} have not been downloaded "
            "after it finished; run `podcast produce` first"
        )
    return CleanupPlan(current=current, candidates=candidates, blocked_reason=blocked)


def describe_record(record: AuphonicProductionRecord) -> str:
    parts = []
    if record.created_at is not None:
        parts.append(f"created {record.created_at.isoformat(timespec='seconds')}")
    if record.last_known_state is not None:
        parts.append(f"last seen {record.last_known_state}")
    if record.superseded_reason:
        parts.append(f"superseded: {record.superseded_reason}")
    if record.superseded_by:
        parts.append(f"by {record.superseded_by}")
    return f"{record.uuid} ({'; '.join(parts)})" if parts else record.uuid
