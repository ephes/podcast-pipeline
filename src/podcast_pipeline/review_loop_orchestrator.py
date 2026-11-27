from __future__ import annotations

import fcntl
import os
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
    load_loop_protocol_state,
    run_review_loop_engine,
)
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore, atomic_write_text

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
    store = EpisodeWorkspaceStore(workspace)
    lock_dir = workspace / ".review-locks" / store.layout.protocol_state_json_path(asset_id).parent.name
    with _review_lock(lock_dir, asset_id):
        return _run_review_loop(
            store=store,
            asset_id=asset_id,
            max_iterations=max_iterations,
            creator=creator,
            reviewer=reviewer,
            seed_candidate=seed_candidate,
        )


@contextmanager
def _review_lock(lock_dir: Path, asset_id: str) -> Iterator[None]:
    try:
        # Keep the directory inode stable even when the protocol is archived.
        lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_dir, os.O_RDONLY | os.O_DIRECTORY)
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
) -> LoopProtocolState:
    layout = store.layout
    protocol_path = layout.protocol_state_json_path(asset_id)
    existing = (
        load_loop_protocol_state(protocol_path) if protocol_path.is_symlink() or protocol_path.exists() else None
    )
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
    if existing is not None and any(
        saved_by_id.get(it.candidate.candidate_id) != it.candidate for it in existing.iterations
    ):
        raise LoopProtocolError("Invalid review protocol state; saved candidate evidence is missing or changed.")
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

    return protocol_state


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
