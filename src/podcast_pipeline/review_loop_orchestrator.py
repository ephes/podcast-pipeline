from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID

from podcast_pipeline.control_json import MAX_JSON_BYTES
from podcast_pipeline.domain.models import (
    Asset,
    AssetKind,
    Candidate,
    EpisodeWorkspace,
    IssueSeverity,
    ReviewIssue,
    ReviewIteration,
    ReviewVerdict,
    TextFormat,
)
from podcast_pipeline.file_inputs import read_workspace_utf8
from podcast_pipeline.pick_core import build_asset, clear_manual_selection_provenance
from podcast_pipeline.protocol_schemas import parse_candidate_json
from podcast_pipeline.review_loop_engine import (
    CreatorInput,
    CreatorOutput,
    LoopDecision,
    LoopOutcome,
    LoopProtocolError,
    LoopProtocolState,
    ProtocolWrite,
    ReviewerInput,
    load_loop_protocol_state_with_body,
    run_review_loop_engine,
    unique_json_members,
)
from podcast_pipeline.workspace_store import (
    EpisodeWorkspaceLayout,
    EpisodeWorkspaceStore,
    WorkspaceStoreError,
    atomic_write_text,
)

MAX_CANDIDATE_EVIDENCE_FILE_BYTES = MAX_JSON_BYTES
MAX_CANDIDATE_EVIDENCE_TOTAL_BYTES = 64 * 1024 * 1024


class ReviewSelectionConflict(ValueError):
    """Completed evidence was retained, but a producer changed the reviewed selection."""


def _selection_generation(workspace: EpisodeWorkspace, asset_id: str, texts: dict[str, str]) -> dict[str, Any]:
    asset = next((asset for asset in workspace.assets if asset.asset_id == asset_id), None)
    return {
        "selected_candidate_id": None if asset is None else asset.selected_candidate_id,
        "manual_provenance": [
            ref.model_dump(mode="json")
            for ref in workspace.provenance
            if ref.kind == "manual_selection" and ref.ref == asset_id
        ],
        "texts": texts,
    }


@dataclass
class _SeededCreator:
    seed_candidate: Candidate
    creator: Callable[[CreatorInput], CreatorOutput]
    _used: bool = False

    def __call__(self, inp: CreatorInput) -> CreatorOutput:
        if not self._used and inp.previous_candidate is None:
            self._used = True
            seeded_input = CreatorInput(
                asset_id=inp.asset_id,
                iteration=inp.iteration,
                previous_candidate=self.seed_candidate.model_copy(deep=True),
                previous_review=inp.previous_review,
            )
            return self.creator(seeded_input)
        return self.creator(inp)


_LOCKED_DECISION_ASSETS = frozenset(
    {
        AssetKind.slug.value,
        AssetKind.title_detail.value,
        AssetKind.title_seo.value,
        AssetKind.subtitle_auphonic.value,
    },
)


def _normalize_selected_text(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _locked_selection_path(
    *,
    store: EpisodeWorkspaceStore,
    asset_id: str,
    fmt: TextFormat,
) -> Path | None:
    if asset_id not in _LOCKED_DECISION_ASSETS:
        return None
    # Prefer the candidate's own format; a custom selection may be stored in another one.
    for candidate_fmt in (fmt, *(other for other in TextFormat if other != fmt)):
        path = store.layout.selected_text_path(asset_id, candidate_fmt)
        if path.exists():
            return path
    return None


def _apply_locked_selection_issue(
    *,
    review: ReviewIteration,
    asset_id: str,
    selected_path: Path,
) -> ReviewIteration:
    issue = ReviewIssue(
        severity=IssueSeverity.error,
        code="locked_selection",
        field="content",
        message=(f"Selected content for {asset_id} is locked after pick; keep it unchanged (see {selected_path})."),
    )
    issues = list(review.issues)
    issues.append(issue)
    verdict = review.verdict
    if verdict == ReviewVerdict.ok:
        verdict = ReviewVerdict.changes_requested
    return review.model_copy(update={"issues": issues, "verdict": verdict})


def _wrap_reviewer_with_locked_decisions(
    *,
    store: EpisodeWorkspaceStore,
    reviewer: Callable[[ReviewerInput], ReviewIteration],
) -> Callable[[ReviewerInput], ReviewIteration]:
    def wrapped(inp: ReviewerInput) -> ReviewIteration:
        review = reviewer(inp)
        selected_path = _locked_selection_path(store=store, asset_id=inp.asset_id, fmt=inp.candidate.format)
        if selected_path is None:
            return review
        locked = _normalize_selected_text(selected_path.read_text(encoding="utf-8"))
        candidate_text = _normalize_selected_text(inp.candidate.content)
        if candidate_text == locked:
            return review
        return _apply_locked_selection_issue(review=review, asset_id=inp.asset_id, selected_path=selected_path)

    return wrapped


def run_review_loop_orchestrator(
    *,
    workspace: Path,
    asset_id: str,
    max_iterations: int,
    creator: Callable[[CreatorInput], CreatorOutput],
    reviewer: Callable[[ReviewerInput], ReviewIteration],
    seed_candidate: Candidate | None = None,
) -> LoopProtocolState:
    protocol_state, _ = run_review_loop_orchestrator_with_previous(
        workspace=workspace,
        asset_id=asset_id,
        max_iterations=max_iterations,
        creator=creator,
        reviewer=reviewer,
        seed_candidate=seed_candidate,
    )
    return protocol_state


def run_review_loop_orchestrator_with_previous(
    *,
    workspace: Path,
    asset_id: str,
    max_iterations: int,
    creator: Callable[[CreatorInput], CreatorOutput],
    reviewer: Callable[[ReviewerInput], ReviewIteration],
    seed_candidate: Candidate | None = None,
) -> tuple[LoopProtocolState, LoopProtocolState | None]:
    """Run the loop and also return the checkpoint it resumed, read under the review lock."""
    store = EpisodeWorkspaceStore(workspace)
    lock_name = store.layout.protocol_state_json_path(asset_id).parent.name
    with _review_lock(workspace, lock_name, asset_id):
        return _run_review_loop(
            store=store,
            asset_id=asset_id,
            max_iterations=max_iterations,
            creator=creator,
            reviewer=reviewer,
            seed_candidate=seed_candidate,
        )


_LOCKS_DIR_NAME = ".review-locks"


def _open_lock_child(parent_fd: int, name: str) -> int:
    """Create and open a real directory below ``parent_fd`` without following symlinks."""
    try:
        os.mkdir(name, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)


@contextmanager
def _review_lock(workspace: Path, lock_name: str, asset_id: str) -> Iterator[None]:
    lock_dir = workspace / _LOCKS_DIR_NAME / lock_name
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        root_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
        try:
            locks_fd = _open_lock_child(root_fd, _LOCKS_DIR_NAME)
        finally:
            os.close(root_fd)
        try:
            # Keep the directory inode stable even when the protocol is archived.
            fd = _open_lock_child(locks_fd, lock_name)
        finally:
            os.close(locks_fd)
    except OSError as exc:
        raise LoopProtocolError(f"Cannot open review lock at {lock_dir}; check the workspace and retry.") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LoopProtocolError(
                f"Review for {asset_id} is already running; wait for it to finish and retry."
            ) from exc
        yield
    finally:
        os.close(fd)


def _run_review_loop(
    *,
    store: EpisodeWorkspaceStore,
    asset_id: str,
    max_iterations: int,
    creator: Callable[[CreatorInput], CreatorOutput],
    reviewer: Callable[[ReviewerInput], ReviewIteration],
    seed_candidate: Candidate | None,
) -> tuple[LoopProtocolState, LoopProtocolState | None]:
    layout = store.layout
    protocol_path = layout.protocol_state_json_path(asset_id)
    existing, existing_body = _load_existing_checkpoint(protocol_path)
    default = EpisodeWorkspace(episode_id=_episode_id_from_yaml(store), root_dir=".")
    selected_workspace, selected_texts = store.read_selection_snapshot(asset_id, default=default)
    selection_generation = _selection_generation(selected_workspace, asset_id, selected_texts)
    reviewer_runner = _wrap_reviewer_with_locked_decisions(store=store, reviewer=reviewer)

    try:
        saved_candidates = _load_seed_candidates(layout, asset_id)
    except (OSError, ValueError) as exc:
        # Content-free diagnostic; the cause stays chained for debugging.
        raise LoopProtocolError(
            f"Invalid saved review candidate evidence; repair or archive copy/candidates/{asset_id}/."
        ) from exc
    saved_by_id = {candidate.candidate_id: candidate for candidate in saved_candidates}
    if existing is not None:
        if any(saved_by_id.get(it.candidate.candidate_id) != it.candidate for it in existing.iterations):
            raise LoopProtocolError("Invalid review protocol state; saved candidate evidence is missing or changed.")
        _verify_saved_reviews(store, asset_id, existing, _saved_checkpoint_reviews(existing_body, existing))
    if seed_candidate is None:
        seed_candidate = _select_seed_candidate(saved_candidates)

    creator_runner: Callable[[CreatorInput], CreatorOutput]
    if seed_candidate is None:
        creator_runner = creator
    else:
        creator_runner = _SeededCreator(seed_candidate=seed_candidate, creator=creator)

    seeded_creator = creator_runner
    known = {candidate.candidate_id: candidate for candidate in saved_candidates}

    def immutable_creator(inp: CreatorInput) -> CreatorOutput:
        output = seeded_creator(inp)
        previous = known.get(output.candidate.candidate_id)
        if previous is not None and previous != output.candidate:
            raise ValueError("Review candidate identity cannot change; use a fresh candidate UUID.")
        return output

    def checkpoint(state: LoopProtocolState, writes: tuple[ProtocolWrite, ...]) -> None:
        marker = writes[-1]
        marker_text = marker.dumps()
        # Publish dependencies first. The protocol state is the durable resume marker.
        _write_protocol_files(writes[:-1])
        try:
            _write_loop_artifacts(
                store=store, asset_id=asset_id, protocol_state=state, selection_generation=selection_generation
            )
        except ReviewSelectionConflict:
            conflicted = replace(
                state,
                decision=LoopDecision(
                    outcome=LoopOutcome.needs_human,
                    final_iteration=state.iterations[-1].iteration,
                    reason="selection_conflict",
                    locked_fields=frozenset({"outcome", "final_iteration", "reason"}),
                ),
            )
            _write_protocol_files((ProtocolWrite(protocol_path, conflicted.to_json_dict()),))
            raise
        atomic_write_text(marker.path, marker_text)

    protocol_state, _ = run_review_loop_engine(
        layout=layout,
        asset_id=asset_id,
        max_iterations=max_iterations,
        creator=immutable_creator,
        reviewer=reviewer_runner,
        existing=existing,
        checkpoint=checkpoint,
    )

    return protocol_state, existing


def _load_existing_checkpoint(protocol_path: Path) -> tuple[LoopProtocolState | None, bytes]:
    if not (protocol_path.is_symlink() or protocol_path.exists()):
        return None, b""
    # One read: the sidecar check compares with exactly these checkpoint bytes.
    return load_loop_protocol_state_with_body(protocol_path)


def _saved_checkpoint_reviews(body: bytes, state: LoopProtocolState) -> list[Any]:
    """Return each round's review exactly as the checkpoint file stores it, numbers kept as text."""
    try:
        rounds = _lossless_json(body)["iterations"]
        if not isinstance(rounds, list) or len(rounds) != len(state.iterations):
            raise ValueError("checkpoint rounds changed")
        return [entry["reviewer"] for entry in rounds]
    except (ValueError, TypeError, KeyError, RecursionError):
        raise LoopProtocolError("Invalid review protocol state; restore or repair the saved evidence.") from None


def _verify_saved_reviews(
    store: EpisodeWorkspaceStore, asset_id: str, state: LoopProtocolState, saved_reviews: list[Any]
) -> None:
    for iteration, saved_review in zip(state.iterations, saved_reviews, strict=True):
        number = iteration.iteration
        reviewer = iteration.review.reviewer
        try:
            path = store.layout.review_iteration_json_path(asset_id, number, reviewer=reviewer)
        except ValueError as exc:
            raise LoopProtocolError(
                f"Invalid review protocol state; repair the reviewer identity for round {number}."
            ) from exc
        message = f"Review at {path} is missing, invalid or changed; restore the matching file before retrying."
        try:
            saved, text = store.read_review_document(asset_id, number, reviewer=reviewer)
            matches = _matches_checkpoint(saved, text, iteration.review, saved_review)
        except (OSError, WorkspaceStoreError, ValueError, TypeError, RecursionError) as exc:
            raise LoopProtocolError(message) from exc
        if not matches:
            raise LoopProtocolError(message)


def _canonical_json(model: ReviewIteration) -> str:
    # Canonical JSON keeps types apart: Python treats ``True == 1`` and ``1 == 1.0``, JSON text does not.
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, allow_nan=False)


def _lossless_json(text: str | bytes) -> Any:
    # Keep number tokens as text: ``1.0`` and ``1.0000000000000001`` decode to the same float.
    return json.loads(
        text,
        object_pairs_hook=unique_json_members,
        parse_constant=_reject_constant,
        parse_float=lambda token: ("float", token),
        parse_int=lambda token: ("int", token),
    )


def _reject_constant(_: str) -> Any:
    raise ValueError("nonfinite JSON number")


def _matches_checkpoint(saved: ReviewIteration, text: str, expected: ReviewIteration, persisted: Any) -> bool:
    # Omitted fields of older files keep their defaults; the validated values must still agree.
    if _canonical_json(saved) != _canonical_json(expected):
        return False
    # Validation coerces and truncates (``"iteration": true`` becomes ``1``, a seventh fraction digit
    # is dropped), so every value the file states must equal the value the checkpoint file stores,
    # token by token. Compare with the stored text, not a reserialization, which may respell it.
    return _states_only_saved_values(_lossless_json(text), persisted)


def _states_only_saved_values(stated: Any, saved: Any) -> bool:
    if isinstance(stated, dict):
        return isinstance(saved, dict) and all(
            key in saved and _states_only_saved_values(value, saved[key]) for key, value in stated.items()
        )
    if isinstance(stated, list):
        return (
            isinstance(saved, list)
            and len(stated) == len(saved)
            and all(_states_only_saved_values(a, b) for a, b in zip(stated, saved, strict=True))
        )
    if isinstance(stated, str) and isinstance(saved, str) and stated != saved:
        return _legacy_utc_spelling(stated, saved)
    return type(stated) is type(saved) and stated == saved


_ISO_UTC_PREFIX = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?")


def _legacy_utc_spelling(stated: str, saved: str) -> bool:
    # Before sub-minute offsets were saved in UTC, a -30 s offset was written as ``-00:00``. Continuing
    # such a run rewrites the checkpoint as ``Z`` while the numbered review keeps ``-00:00``.
    # Accept only that suffix with an identical timestamp prefix, so no digit can change.
    prefix = saved.removesuffix("Z")
    return saved.endswith("Z") and stated == prefix + "-00:00" and _ISO_UTC_PREFIX.fullmatch(prefix) is not None


def _load_seed_candidates(layout: EpisodeWorkspaceLayout, asset_id: str) -> list[Candidate]:
    candidates_dir = layout.copy_candidates_dir / asset_id
    try:
        entries = os.scandir(candidates_dir)
    except FileNotFoundError:
        return []

    remaining = MAX_CANDIDATE_EVIDENCE_TOTAL_BYTES
    candidates: list[Candidate] = []
    with entries:
        for entry in entries:
            if not (entry.name.startswith("candidate_") and entry.name.endswith(".json")):
                continue
            path = candidates_dir / entry.name
            raw = read_workspace_utf8(
                layout.root, path.relative_to(layout.root), max_bytes=min(MAX_CANDIDATE_EVIDENCE_FILE_BYTES, remaining)
            )
            remaining -= len(raw.encode("utf-8"))
            candidate = parse_candidate_json(raw)
            if candidate.asset_id != asset_id:
                raise ValueError(f"candidate asset_id mismatch: {path}")
            if path != layout.candidate_json_path(asset_id, candidate.candidate_id):
                raise ValueError(f"candidate filename does not match its UUID: {path}")
            candidates.append(candidate)
    return candidates


def _select_seed_candidate(candidates: Sequence[Candidate]) -> Candidate | None:
    if not candidates:
        return None
    return max(candidates, key=lambda cand: (cand.created_at, str(cand.candidate_id)))


def _write_protocol_files(writes: tuple[ProtocolWrite, ...]) -> None:
    documents = [(write.path, write.dumps()) for write in writes]
    for path, text in documents:
        atomic_write_text(path, text)


def _write_loop_artifacts(
    *,
    store: EpisodeWorkspaceStore,
    asset_id: str,
    protocol_state: LoopProtocolState,
    selection_generation: dict[str, Any],
) -> None:
    for iteration in protocol_state.iterations[-1:]:
        store.write_candidate(iteration.candidate, immutable=True)
        store.write_review(asset_id, iteration.review)

    selected_candidate_id = None
    if protocol_state.iterations:
        final_iteration = protocol_state.iterations[-1]
        if protocol_state.decision is not None and protocol_state.decision.outcome == LoopOutcome.converged:
            selected_candidate_id = final_iteration.candidate.candidate_id

    _write_workspace_state(
        store=store,
        asset_id=asset_id,
        protocol_state=protocol_state,
        selected_candidate_id=selected_candidate_id,
        selection_generation=selection_generation,
    )


def _write_workspace_state(
    *,
    store: EpisodeWorkspaceStore,
    asset_id: str,
    protocol_state: LoopProtocolState,
    selected_candidate_id: UUID | None,
    selection_generation: dict[str, Any],
) -> None:
    conflict = False

    def update(workspace: EpisodeWorkspace) -> EpisodeWorkspace:
        nonlocal conflict
        texts = {
            fmt.value: path.read_text(encoding="utf-8")
            for fmt in TextFormat
            if (path := store.layout.selected_text_path(asset_id, fmt)).exists()
        }
        conflict = (
            selected_candidate_id is not None
            and _selection_generation(workspace, asset_id, texts) != selection_generation
        )
        effective_selection = None if conflict else selected_candidate_id
        existing = next((asset for asset in workspace.assets if asset.asset_id == asset_id), None)
        # A loop needing human input updates its candidates, not the published selection.
        selected = (
            effective_selection
            if effective_selection is not None
            else (existing.selected_candidate_id if existing is not None else None)
        )
        asset = build_asset(
            asset_id=asset_id,
            existing=existing,
            candidates=[it.candidate for it in protocol_state.iterations],
            selected_candidate_id=selected,
        ).model_copy(update={"reviews": [it.review for it in protocol_state.iterations]})
        if effective_selection is not None:
            final_candidate = protocol_state.iterations[-1].candidate
            store.write_selected_text(asset_id, final_candidate.format, final_candidate.content)
            workspace = clear_manual_selection_provenance(workspace, asset_id)
        assets = _upsert_asset(list(workspace.assets), asset)
        return workspace.model_copy(update={"assets": assets})

    default = EpisodeWorkspace(episode_id=_episode_id_from_yaml(store), root_dir=".")
    paths = [store.layout.selected_text_path(asset_id, fmt) for fmt in TextFormat]
    store.update_state(update, default=default, artifact_paths=paths)
    if conflict:
        raise ReviewSelectionConflict(
            "Reviewed selection changed; candidates retained. Check the producer selection before a fresh run."
        )


def _episode_id_from_yaml(store: EpisodeWorkspaceStore) -> str:
    if store.layout.episode_yaml.exists():
        data = store.read_episode_yaml()
        episode_id = data.get("episode_id")
        if isinstance(episode_id, str) and episode_id.strip():
            return episode_id
    return store.layout.root.name


def _upsert_asset(assets: list[Asset], updated: Asset) -> list[Asset]:
    replaced = False
    out: list[Asset] = []
    for asset in assets:
        if asset.asset_id == updated.asset_id:
            out.append(updated)
            replaced = True
        else:
            out.append(asset)
    if not replaced:
        out.append(updated)
    return out
