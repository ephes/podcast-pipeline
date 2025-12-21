from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

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
from podcast_pipeline.review_loop_engine import (
    CreatorInput,
    CreatorOutput,
    LoopOutcome,
    LoopProtocolState,
    ProtocolWrite,
    ReviewerInput,
    run_review_loop_engine,
)
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore


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
                previous_candidate=self.seed_candidate,
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


def _locked_selected_text(
    *,
    store: EpisodeWorkspaceStore,
    asset_id: str,
    fmt: TextFormat,
) -> str | None:
    if asset_id not in _LOCKED_DECISION_ASSETS:
        return None
    path = store.layout.selected_text_path(asset_id, fmt)
    if not path.exists():
        return None
    return _normalize_selected_text(path.read_text(encoding="utf-8"))


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
        locked = _locked_selected_text(store=store, asset_id=inp.asset_id, fmt=inp.candidate.format)
        if locked is None:
            return review
        candidate_text = _normalize_selected_text(inp.candidate.content)
        if candidate_text == locked:
            return review
        selected_path = store.layout.selected_text_path(inp.asset_id, inp.candidate.format)
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
    layout = store.layout
    reviewer_runner = _wrap_reviewer_with_locked_decisions(store=store, reviewer=reviewer)

    if seed_candidate is None:
        seed_candidate = _select_seed_candidate(_load_seed_candidates(layout, asset_id))

    creator_runner: Callable[[CreatorInput], CreatorOutput]
    if seed_candidate is None:
        creator_runner = creator
    else:
        creator_runner = _SeededCreator(seed_candidate=seed_candidate, creator=creator)

    protocol_state, protocol_writes = run_review_loop_engine(
        layout=layout,
        asset_id=asset_id,
        max_iterations=max_iterations,
        creator=creator_runner,
        reviewer=reviewer_runner,
    )

    _write_protocol_files(protocol_writes)
    _write_loop_artifacts(store=store, asset_id=asset_id, protocol_state=protocol_state)
    return protocol_state


def _load_seed_candidates(layout: EpisodeWorkspaceLayout, asset_id: str) -> list[Candidate]:
    candidates_dir = layout.copy_candidates_dir / asset_id
    if not candidates_dir.exists():
        return []

    candidates: list[Candidate] = []
    for path in sorted(candidates_dir.glob("candidate_*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        candidate = Candidate.model_validate(raw)
        if candidate.asset_id != asset_id:
            raise ValueError(f"candidate asset_id mismatch: {path}")
        candidates.append(candidate)
    return candidates


def _select_seed_candidate(candidates: Sequence[Candidate]) -> Candidate | None:
    if not candidates:
        return None
    return max(candidates, key=lambda cand: (cand.created_at, str(cand.candidate_id)))


def _write_protocol_files(writes: tuple[ProtocolWrite, ...]) -> None:
    for write in writes:
        write.path.parent.mkdir(parents=True, exist_ok=True)
        write.path.write_text(write.dumps(), encoding="utf-8")


def _write_loop_artifacts(
    *,
    store: EpisodeWorkspaceStore,
    asset_id: str,
    protocol_state: LoopProtocolState,
) -> None:
    for iteration in protocol_state.iterations:
        store.write_candidate(iteration.candidate)
        store.write_review(asset_id, iteration.review)

    selected_candidate_id = None
    if protocol_state.iterations:
        final_iteration = protocol_state.iterations[-1]
        if protocol_state.decision is not None and protocol_state.decision.outcome == LoopOutcome.converged:
            store.write_selected_text(
                asset_id,
                final_iteration.candidate.format,
                final_iteration.candidate.content,
            )
            selected_candidate_id = final_iteration.candidate.candidate_id

    _write_workspace_state(
        store=store,
        asset_id=asset_id,
        protocol_state=protocol_state,
        selected_candidate_id=selected_candidate_id,
    )


def _write_workspace_state(
    *,
    store: EpisodeWorkspaceStore,
    asset_id: str,
    protocol_state: LoopProtocolState,
    selected_candidate_id: UUID | None,
) -> None:
    workspace = _load_workspace(store)
    kind = _asset_kind(asset_id)
    asset = Asset(
        asset_id=asset_id,
        kind=kind,
        candidates=[it.candidate for it in protocol_state.iterations],
        reviews=[it.review for it in protocol_state.iterations],
        selected_candidate_id=selected_candidate_id,
    )
    assets = _upsert_asset(list(workspace.assets), asset)
    updated = workspace.model_copy(update={"assets": assets})
    store.write_state(updated)


def _load_workspace(store: EpisodeWorkspaceStore) -> EpisodeWorkspace:
    if store.layout.state_json.exists():
        return store.read_state()
    episode_id = _episode_id_from_yaml(store)
    return EpisodeWorkspace(episode_id=episode_id, root_dir=".")


def _episode_id_from_yaml(store: EpisodeWorkspaceStore) -> str:
    if store.layout.episode_yaml.exists():
        data = store.read_episode_yaml()
        episode_id = data.get("episode_id")
        if isinstance(episode_id, str) and episode_id.strip():
            return episode_id
    return store.layout.root.name


def _asset_kind(asset_id: str) -> AssetKind | None:
    try:
        return AssetKind(asset_id)
    except ValueError:
        return None


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
