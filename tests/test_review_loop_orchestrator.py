from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from podcast_pipeline.agent_runners import FakeCreatorRunner, FakeReviewerRunner
from podcast_pipeline.domain.models import (
    Candidate,
    EpisodeWorkspace,
    ProvenanceRef,
    ReviewIteration,
    ReviewVerdict,
    TextFormat,
)
from podcast_pipeline.review_loop_engine import (
    CreatorOutput,
    LoopOutcome,
    LoopProtocolError,
    LoopProtocolState,
    ReviewerInput,
)
from podcast_pipeline.review_loop_orchestrator import run_review_loop_orchestrator
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def _init_workspace(tmp_path: Path) -> EpisodeWorkspaceStore:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep_001", "inputs": {}})
    store.write_state(EpisodeWorkspace(episode_id="ep_001", root_dir="."))
    return store


def test_orchestrator_runs_with_seed_and_writes_protocol_files(tmp_path: Path) -> None:
    store = _init_workspace(tmp_path)
    seed_candidate = Candidate(asset_id="description", content="seed")
    store.write_candidate(seed_candidate)

    creator = FakeCreatorRunner(
        layout=store.layout,
        replies=[{"done": True, "candidate": {"content": "final copy"}}],
    )
    reviewer = FakeReviewerRunner(
        layout=store.layout,
        reviewer="reviewer_a",
        replies=[{"verdict": "ok"}],
    )

    protocol_state = run_review_loop_orchestrator(
        workspace=store.layout.root,
        asset_id="description",
        max_iterations=3,
        creator=creator,
        reviewer=reviewer,
    )

    assert protocol_state.decision is not None
    assert protocol_state.decision.outcome == LoopOutcome.converged
    assert len(protocol_state.iterations) == 1
    assert store.layout.protocol_iteration_json_path("description", 1).exists()
    assert store.layout.protocol_state_json_path("description").exists()
    assert store.layout.review_iteration_json_path("description", 1, reviewer="reviewer_a").exists()
    assert store.layout.selected_text_path("description", protocol_state.iterations[-1].candidate.format).exists()

    assert creator.calls
    assert creator.calls[0].previous_candidate is not None
    assert creator.calls[0].previous_candidate.candidate_id == seed_candidate.candidate_id


def test_orchestrator_stops_at_iteration_limit(tmp_path: Path) -> None:
    store = _init_workspace(tmp_path)
    seed_candidate = Candidate(asset_id="description", content="seed")
    store.write_candidate(seed_candidate)

    creator = FakeCreatorRunner(
        layout=store.layout,
        replies=[
            {"done": False, "candidate": {"content": "draft 1"}},
            {"done": False, "candidate": {"content": "draft 2"}},
        ],
    )
    reviewer = FakeReviewerRunner(
        layout=store.layout,
        reviewer="reviewer_a",
        replies=[
            {"verdict": "changes_requested", "issues": [{"message": "add more detail"}]},
            {"verdict": "changes_requested", "issues": [{"message": "still needs edits"}]},
        ],
    )

    protocol_state = run_review_loop_orchestrator(
        workspace=store.layout.root,
        asset_id="description",
        max_iterations=2,
        creator=creator,
        reviewer=reviewer,
    )

    assert protocol_state.decision is not None
    assert protocol_state.decision.outcome == LoopOutcome.needs_human
    assert len(protocol_state.iterations) == 2
    assert store.layout.protocol_iteration_json_path("description", 2).exists()

    workspace_state = store.read_state()
    asset = next(asset for asset in workspace_state.assets if asset.asset_id == "description")
    assert asset.selected_candidate_id is None


def test_orchestrator_rejects_locked_selection_changes(tmp_path: Path) -> None:
    store = _init_workspace(tmp_path)
    store.write_selected_text("slug", TextFormat.markdown, "# Slug\n\nlocked\n")
    seed_candidate = Candidate(asset_id="slug", content="# Slug\n\nlocked\n")
    store.write_candidate(seed_candidate)

    creator = FakeCreatorRunner(
        layout=store.layout,
        replies=[{"done": True, "candidate": {"content": "# Slug\n\nchanged\n"}}],
    )
    reviewer = FakeReviewerRunner(
        layout=store.layout,
        reviewer="reviewer_a",
        replies=[{"verdict": "ok"}],
    )

    protocol_state = run_review_loop_orchestrator(
        workspace=store.layout.root,
        asset_id="slug",
        max_iterations=1,
        creator=creator,
        reviewer=reviewer,
    )

    assert protocol_state.decision is not None
    assert protocol_state.decision.outcome == LoopOutcome.needs_human

    review = store.read_review("slug", 1, reviewer="reviewer_a")
    assert review.verdict == ReviewVerdict.changes_requested
    assert any(issue.code == "locked_selection" for issue in review.issues)


def test_completed_iteration_survives_later_creator_failure_and_resumes(tmp_path: Path) -> None:
    import json

    import pytest

    from podcast_pipeline.review_loop_engine import CreatorInput, CreatorOutput

    store = _init_workspace(tmp_path)
    first = Candidate(asset_id="description", content="completed paid draft")

    def creator(inp: CreatorInput) -> CreatorOutput:
        if inp.iteration == 2:
            raise RuntimeError("synthetic creator failure")
        return CreatorOutput(candidate=first, done=False)

    reviewer = FakeReviewerRunner(
        layout=store.layout, reviewer="synthetic", replies=[{"verdict": "changes_requested"}]
    )
    with pytest.raises(RuntimeError, match="synthetic creator failure"):
        run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
        )
    reopened = EpisodeWorkspaceStore(tmp_path)
    raw = json.loads(reopened.layout.protocol_state_json_path("description").read_text())
    assert [it["iteration"] for it in raw["iterations"]] == [1]
    assert raw["decision"] is None
    assert not reopened.layout.protocol_iteration_json_path("description", 2).exists()
    assert reopened.read_candidate("description", first.candidate_id) == first
    assert reopened.read_review("description", 1, reviewer="synthetic").verdict == ReviewVerdict.changes_requested

    continuation = FakeCreatorRunner(layout=store.layout, replies=[{"done": True, "candidate": {"content": "final"}}])
    final_reviewer = FakeReviewerRunner(layout=store.layout, reviewer="synthetic", replies=[{"verdict": "ok"}])
    result = run_review_loop_orchestrator(
        workspace=tmp_path, asset_id="description", max_iterations=3, creator=continuation, reviewer=final_reviewer
    )
    assert [call.iteration for call in continuation.calls] == [2]
    assert continuation.calls[0].previous_candidate == first
    assert len(result.iterations) == 2


def test_terminal_resume_makes_no_calls_or_selection_changes(tmp_path: Path) -> None:
    from podcast_pipeline.pick_core import save_custom_selection

    store = _init_workspace(tmp_path)
    creator = FakeCreatorRunner(layout=store.layout, replies=[{"done": True, "candidate": {"content": "final"}}])
    reviewer = FakeReviewerRunner(layout=store.layout, reviewer="synthetic", replies=[{"verdict": "ok"}])
    first = run_review_loop_orchestrator(
        workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
    )
    save_custom_selection(
        store=store, workspace=store.read_state(), asset_id="description", candidates=[], text="manual"
    )
    state_before = store.layout.state_json.read_bytes()
    second = run_review_loop_orchestrator(
        workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
    )
    assert second == first
    assert len(creator.calls) == len(reviewer.calls) == 1
    assert store.layout.state_json.read_bytes() == state_before
    assert store.layout.selected_text_path("description", TextFormat.markdown).read_text() == "manual\n"


@pytest.mark.parametrize("sidecar", ["changed", "missing", "invalid", "legacy"])
def test_resume_checks_every_committed_review_sidecar(tmp_path: Path, sidecar: str) -> None:
    store = _init_workspace(tmp_path)
    creator = FakeCreatorRunner(
        layout=store.layout,
        replies=[
            {"done": False, "candidate": {"content": "first"}},
            {"done": True, "candidate": {"content": "final"}},
        ],
    )
    first_reviewer = None if sidecar == "legacy" else "first_reviewer"

    def reviewer(inp: ReviewerInput) -> ReviewIteration:
        if inp.iteration == 1:
            return ReviewIteration(iteration=1, reviewer=first_reviewer, verdict=ReviewVerdict.changes_requested)
        return ReviewIteration(iteration=2, reviewer="final_reviewer", verdict=ReviewVerdict.ok)

    original = run_review_loop_orchestrator(
        workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
    )
    path = store.layout.review_iteration_json_path("description", 1, reviewer=first_reviewer)
    if sidecar == "missing":
        path.unlink()
    elif sidecar == "invalid":
        path.write_text('{"verdict":"ok",' + path.read_text()[1:])
    else:
        raw = json.loads(path.read_text())
        if sidecar == "changed":
            raw["verdict"] = ReviewVerdict.needs_human.value
        else:
            del raw["reviewer"], raw["provenance"]
        path.write_text(json.dumps(raw))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    no_creator = FakeCreatorRunner(layout=store.layout, replies=[])
    no_reviewer = FakeReviewerRunner(layout=store.layout, reviewer="unused", replies=[])

    def resume() -> LoopProtocolState:
        return run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=3, creator=no_creator, reviewer=no_reviewer
        )

    if sidecar == "legacy":
        assert resume() == original
    else:
        with pytest.raises(
            LoopProtocolError, match=r"copy/reviews/description/iteration_01\.first_reviewer\.json.*restore"
        ):
            resume()
    assert not no_creator.calls and not no_reviewer.calls
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_review_ownership_is_per_asset_across_processes(tmp_path: Path) -> None:
    store = _init_workspace(tmp_path)
    script = """
import sys
from pathlib import Path
from podcast_pipeline.domain.models import Candidate, ReviewIteration, ReviewVerdict
from podcast_pipeline.review_loop_engine import CreatorOutput
from podcast_pipeline.review_loop_orchestrator import run_review_loop_orchestrator

def creator(inp):
    if inp.iteration == 2:
        print("ready", flush=True)
        assert sys.stdin.readline().strip() == "finish"
    return CreatorOutput(Candidate(asset_id=inp.asset_id, content=f"round {inp.iteration}"), inp.iteration == 2)

def reviewer(inp):
    verdict = ReviewVerdict.changes_requested if inp.iteration == 1 else ReviewVerdict.ok
    return ReviewIteration(iteration=inp.iteration, verdict=verdict)

run_review_loop_orchestrator(
    workspace=Path(sys.argv[1]), asset_id="description", max_iterations=2, creator=creator, reviewer=reviewer,
)
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        assert select.select([owner.stdout], [], [], 10)[0]
        assert owner.stdout.readline().strip() == "ready"
        checkpoint = store.layout.protocol_state_json_path("description")
        before = checkpoint.read_bytes()
        creator = FakeCreatorRunner(layout=store.layout, replies=[{"done": True, "candidate": {"content": "other"}}])
        reviewer = FakeReviewerRunner(layout=store.layout, reviewer="synthetic", replies=[{"verdict": "ok"}])

        with pytest.raises(LoopProtocolError, match=r"description.*already running.*retry"):
            run_review_loop_orchestrator(
                workspace=tmp_path, asset_id="description", max_iterations=2, creator=creator, reviewer=reviewer
            )
        assert not creator.calls and not reviewer.calls
        assert checkpoint.read_bytes() == before
        other = run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="shownotes", max_iterations=2, creator=creator, reviewer=reviewer
        )
        assert other.decision is not None and other.decision.outcome == LoopOutcome.converged
        owner.communicate("finish\n", timeout=10)
        assert owner.returncode == 0
        resumed = run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=2, creator=creator, reviewer=reviewer
        )
        assert resumed.decision is not None and resumed.decision.outcome == LoopOutcome.converged
        assert len(resumed.iterations) == 2
        assert len(creator.calls) == len(reviewer.calls) == 1
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=10)


@pytest.mark.parametrize("kind", ["fifo", "dangling-symlink"])
def test_nonregular_protocol_fails_before_callbacks_without_waiting(tmp_path: Path, kind: str) -> None:
    store = _init_workspace(tmp_path)
    path = store.layout.protocol_state_json_path("description")
    path.parent.mkdir(parents=True)
    if kind == "fifo":
        os.mkfifo(path)
    else:
        path.symlink_to("missing.json")
    script = """
import sys
from pathlib import Path
from podcast_pipeline.review_loop_engine import LoopProtocolError
from podcast_pipeline.review_loop_orchestrator import run_review_loop_orchestrator

def forbidden(_):
    raise AssertionError("unexpected review callback")

try:
    run_review_loop_orchestrator(
        workspace=Path(sys.argv[1]), asset_id="description", max_iterations=1,
        creator=forbidden, reviewer=forbidden,
    )
except LoopProtocolError as exc:
    assert "restore or repair" in str(exc)
else:
    raise AssertionError("admitted nonregular protocol state")
"""
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=3, check=True)


def test_malformed_protocol_fails_before_callbacks_or_writes(tmp_path: Path) -> None:
    import pytest

    store = _init_workspace(tmp_path)
    path = store.layout.protocol_state_json_path("description")
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "asset_id": "description"}')
    before = store.layout.state_json.read_bytes()
    creator = FakeCreatorRunner(layout=store.layout, replies=[])
    reviewer = FakeReviewerRunner(layout=store.layout, reviewer="synthetic", replies=[])
    with pytest.raises(ValueError):
        run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
        )
    assert not creator.calls and not reviewer.calls
    assert store.layout.state_json.read_bytes() == before


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_saved_protocol_fails_before_callbacks(tmp_path: Path, token: str) -> None:
    store = _init_workspace(tmp_path)
    candidate = Candidate(
        asset_id="description",
        content="synthetic copy",
        provenance=[ProvenanceRef(kind="test", ref="synthetic", metadata={"score": "SCORE_MARKER"})],
    )
    reviewer = FakeReviewerRunner(layout=store.layout, reviewer="synthetic", replies=[{"verdict": "ok"}])
    run_review_loop_orchestrator(
        workspace=tmp_path,
        asset_id="description",
        max_iterations=1,
        creator=lambda _: CreatorOutput(candidate=candidate, done=True),
        reviewer=reviewer,
    )
    path = store.layout.protocol_state_json_path("description")
    path.write_text(path.read_text(encoding="utf-8").replace('"SCORE_MARKER"', token), encoding="utf-8")
    before = path.read_bytes()
    creator = FakeCreatorRunner(layout=store.layout, replies=[])
    reviewer = FakeReviewerRunner(layout=store.layout, reviewer="synthetic", replies=[])
    with pytest.raises(LoopProtocolError, match="restore or repair"):
        run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=1, creator=creator, reviewer=reviewer
        )
    assert not creator.calls and not reviewer.calls
    assert path.read_bytes() == before


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "negative-inf"])
def test_nonfinite_candidate_cannot_publish_checkpoint(tmp_path: Path, score: float) -> None:
    store = _init_workspace(tmp_path)
    candidate = Candidate(
        asset_id="description",
        content="synthetic copy",
        provenance=[ProvenanceRef(kind="test", ref="synthetic", metadata={"metrics": [{"score": score}]})],
    )
    reviewer = FakeReviewerRunner(layout=store.layout, reviewer="synthetic", replies=[{"verdict": "ok"}])
    before = store.layout.state_json.read_bytes()
    with pytest.raises(LoopProtocolError, match="nonfinite"):
        run_review_loop_orchestrator(
            workspace=tmp_path,
            asset_id="description",
            max_iterations=1,
            creator=lambda _: CreatorOutput(candidate=candidate, done=True),
            reviewer=reviewer,
        )
    assert not store.layout.protocol_state_json_path("description").exists()
    assert not store.layout.protocol_iteration_json_path("description", 1).exists()
    assert not store.layout.candidate_json_path("description", candidate.candidate_id).exists()
    assert store.layout.state_json.read_bytes() == before


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "negative-inf"])
def test_nonfinite_review_cannot_publish_checkpoint(tmp_path: Path, score: float) -> None:
    store = _init_workspace(tmp_path)
    candidate = Candidate(asset_id="description", content="synthetic copy")

    def reviewer(inp: ReviewerInput) -> ReviewIteration:
        return ReviewIteration(
            iteration=inp.iteration,
            verdict=ReviewVerdict.ok,
            reviewer="synthetic",
            provenance=[ProvenanceRef(kind="test", ref="synthetic", metadata={"metrics": [{"score": score}]})],
        )

    before = store.layout.state_json.read_bytes()
    with pytest.raises(LoopProtocolError, match="nonfinite"):
        run_review_loop_orchestrator(
            workspace=tmp_path,
            asset_id="description",
            max_iterations=1,
            creator=lambda _: CreatorOutput(candidate=candidate, done=True),
            reviewer=reviewer,
        )
    assert not store.layout.protocol_state_json_path("description").exists()
    assert not store.layout.protocol_iteration_json_path("description", 1).exists()
    assert not store.layout.candidate_json_path("description", candidate.candidate_id).exists()
    assert store.layout.state_json.read_bytes() == before


@pytest.mark.parametrize("selection", ["custom", "pick", "same_text_custom"])
def test_publication_refuses_a_newer_selection(
    tmp_path: Path, selection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from podcast_pipeline import review_loop_orchestrator
    from podcast_pipeline.pick_core import (
        has_manual_selection_provenance,
        save_candidate_selection,
        save_custom_selection,
    )
    from podcast_pipeline.review_loop_engine import ProtocolWrite

    store = _init_workspace(tmp_path)
    picked = Candidate(asset_id="description", content="producer pick")
    store.write_candidate(picked)
    creator = FakeCreatorRunner(layout=store.layout, replies=[{"done": True, "candidate": {"content": "reviewed"}}])

    reviewer = FakeReviewerRunner(layout=store.layout, reviewer="synthetic", replies=[{"verdict": "ok"}])
    original_write = review_loop_orchestrator._write_protocol_files

    def publish_after_producer_pick(writes: tuple[ProtocolWrite, ...]) -> None:
        # Deterministic barrier after review and before the final state transaction.
        assert len(reviewer.calls) == 1
        if selection == "pick":
            save_candidate_selection(
                store=store,
                workspace=store.read_state(),
                candidate=picked,
                candidates=[picked],
                selected_format=picked.format,
                selected_text=picked.content,
            )
        else:
            save_custom_selection(
                store=store,
                workspace=store.read_state(),
                asset_id="description",
                candidates=[picked],
                text="reviewed" if selection == "same_text_custom" else "producer edit",
            )
        original_write(writes)
        monkeypatch.setattr(review_loop_orchestrator, "_write_protocol_files", original_write)

    monkeypatch.setattr(review_loop_orchestrator, "_write_protocol_files", publish_after_producer_pick)
    with pytest.raises(ValueError, match="selection changed"):
        run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
        )
    current = store.read_state()
    assert has_manual_selection_provenance(current, "description") == (selection != "pick")
    asset = current.assets[0]
    assert asset.selected_candidate_id == (picked.candidate_id if selection == "pick" else None)
    expected = {"pick": "producer pick", "custom": "producer edit", "same_text_custom": "reviewed"}[selection]
    assert store.read_selected_text("description", TextFormat.markdown) == expected + "\n"
    assert len(asset.candidates) == 2
    # The conflict is terminal too; retry must not overwrite the producer's choice.
    result = run_review_loop_orchestrator(
        workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
    )
    assert result.decision is not None and result.decision.reason == "selection_conflict"
    assert len(creator.calls) == 1


@pytest.mark.parametrize(
    "defect",
    ["gap", "reverse", "review", "foreign", "decision", "unlocked", "over_cap", "changed_identity", "missing_id"],
)
def test_inconsistent_resume_fails_before_calls_or_writes(tmp_path: Path, defect: str) -> None:
    import json

    from podcast_pipeline.domain.models import ReviewIteration
    from podcast_pipeline.review_loop_engine import LoopDecision, LoopProtocolIteration, LoopProtocolState

    store = _init_workspace(tmp_path)
    candidate = Candidate(asset_id="description", content="original")
    first = LoopProtocolIteration(
        1, False, candidate, ReviewIteration(iteration=1, verdict=ReviewVerdict.changes_requested)
    )
    second = LoopProtocolIteration(
        2,
        False,
        Candidate(asset_id="description", content="second"),
        ReviewIteration(iteration=2, verdict=ReviewVerdict.changes_requested),
    )
    raw = LoopProtocolState(asset_id="description", max_iterations=3, iterations=(first, second)).to_json_dict()
    if defect == "gap":
        raw["iterations"][1]["iteration"] = 3
    elif defect == "reverse":
        raw["iterations"].reverse()
    elif defect == "review":
        raw["iterations"][0]["reviewer"]["iteration"] = 2
    elif defect == "foreign":
        raw["iterations"][0]["creator"]["candidate"]["asset_id"] = "slug"
    elif defect in ("decision", "unlocked"):
        raw["decision"] = LoopDecision(
            LoopOutcome.needs_human,
            9 if defect == "decision" else 2,
            "selection_conflict",
            frozenset() if defect == "unlocked" else frozenset({"outcome"}),
        ).__dict__
        raw["decision"]["locked_fields"] = sorted(raw["decision"]["locked_fields"])
    elif defect == "over_cap":
        raw["max_iterations"] = 1
    elif defect == "missing_id":
        del raw["iterations"][0]["creator"]["candidate"]["candidate_id"]
    else:
        raw["iterations"][1]["creator"]["candidate"]["candidate_id"] = str(candidate.candidate_id)
    path = store.layout.protocol_state_json_path("description")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(raw))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    creator = FakeCreatorRunner(layout=store.layout, replies=[])
    reviewer = FakeReviewerRunner(layout=store.layout, reviewer="synthetic", replies=[])
    with pytest.raises(ValueError, match="protocol state"):
        run_review_loop_orchestrator(
            workspace=tmp_path,
            asset_id="description",
            max_iterations=raw["max_iterations"],
            creator=creator,
            reviewer=reviewer,
        )
    assert not creator.calls and not reviewer.calls
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_reused_candidate_uuid_never_replaces_paid_evidence(tmp_path: Path) -> None:
    from podcast_pipeline.review_loop_engine import CreatorInput, CreatorOutput

    store = _init_workspace(tmp_path)
    original = Candidate(asset_id="description", content="original evidence")
    snapshot: dict[Path, bytes] = {}
    reviewer = FakeReviewerRunner(
        layout=store.layout, reviewer="synthetic", replies=[{"verdict": "changes_requested"}]
    )

    def creator(inp: CreatorInput) -> CreatorOutput:
        if inp.iteration == 1:
            return CreatorOutput(candidate=original, done=False)
        snapshot.update({p: p.read_bytes() for p in store.layout.copy_dir.rglob("*") if p.is_file()})
        return CreatorOutput(candidate=original.model_copy(update={"content": "changed evidence"}), done=True)

    with pytest.raises(ValueError, match="candidate identity"):
        run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
        )
    assert len(reviewer.calls) == 1
    assert {p: p.read_bytes() for p in store.layout.copy_dir.rglob("*") if p.is_file()} == snapshot
    assert store.read_candidate("description", original.candidate_id) == original


def test_reviewer_human_request_is_terminal_and_keeps_selection(tmp_path: Path) -> None:
    from podcast_pipeline.pick_core import save_custom_selection

    store = _init_workspace(tmp_path)
    save_custom_selection(
        store=store, workspace=store.read_state(), asset_id="description", candidates=[], text="manual"
    )
    creator = FakeCreatorRunner(layout=store.layout, replies=[{"done": False, "candidate": {"content": "reviewed"}}])
    reviewer = FakeReviewerRunner(layout=store.layout, reviewer="synthetic", replies=[{"verdict": "needs_human"}])
    for _ in range(2):
        state = run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
        )
        assert state.decision is not None and state.decision.reason == "reviewer_requested_human"
    assert len(creator.calls) == len(reviewer.calls) == 1
    assert store.read_selected_text("description", TextFormat.markdown) == "manual\n"


def test_changed_saved_candidate_refuses_resume_before_paid_calls(tmp_path: Path) -> None:
    from podcast_pipeline.review_loop_engine import CreatorInput, CreatorOutput

    store = _init_workspace(tmp_path)
    original = Candidate(asset_id="description", content="original evidence")

    def creator(inp: CreatorInput) -> CreatorOutput:
        if inp.iteration > 1:
            raise RuntimeError("synthetic interruption")
        return CreatorOutput(candidate=original, done=False)

    reviewer = FakeReviewerRunner(
        layout=store.layout, reviewer="synthetic", replies=[{"verdict": "changes_requested"}]
    )
    with pytest.raises(RuntimeError, match="interruption"):
        run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
        )
    path = store.layout.candidate_json_path("description", original.candidate_id)
    path.write_text(original.model_copy(update={"content": "changed on disk"}).model_dump_json())
    creator_resume = FakeCreatorRunner(layout=store.layout, replies=[])
    with pytest.raises(ValueError, match="protocol state"):
        run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator_resume, reviewer=reviewer
        )
    assert not creator_resume.calls and len(reviewer.calls) == 1


@pytest.mark.parametrize("keep_canonical", [True, False], ids=["duplicate-uuid", "renamed-file"])
def test_review_refuses_noncanonical_saved_candidate_identity(tmp_path: Path, keep_canonical: bool) -> None:
    store = _init_workspace(tmp_path)
    store.write_selected_text("description", TextFormat.markdown, "previous selection")
    creator = FakeCreatorRunner(layout=store.layout, replies=[{"done": True, "candidate": {"content": "reviewed"}}])
    reviewer = FakeReviewerRunner(layout=store.layout, reviewer="synthetic", replies=[{"verdict": "needs_human"}])
    state = run_review_loop_orchestrator(
        workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
    )
    candidate = state.iterations[0].candidate
    canonical = store.layout.candidate_json_path("description", candidate.candidate_id)
    alias = canonical.with_name("candidate_0.json")
    if keep_canonical:
        alias.write_text(candidate.model_copy(update={"content": "conflicting evidence"}).model_dump_json())
    else:
        canonical.rename(alias)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    with pytest.raises(LoopProtocolError, match=r"repair or archive copy/candidates/description/"):
        run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
        )

    assert len(creator.calls) == len(reviewer.calls) == 1
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("defect", ["per-file", "aggregate", "symlink", "at-limit"])
def test_review_bounds_saved_candidate_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str) -> None:
    from podcast_pipeline import review_loop_orchestrator

    store = _init_workspace(tmp_path)
    store.write_selected_text("description", TextFormat.markdown, "previous selection")
    paths = []
    for _ in range(2):
        candidate = Candidate(asset_id="description", content="Synthetischer Entwurf ä" * 8)
        path = store.write_candidate(candidate)
        path.write_text(candidate.model_dump_json(), encoding="utf-8")
        paths.append(path)
    sizes = [path.stat().st_size for path in paths]
    monkeypatch.setattr(
        review_loop_orchestrator, "MAX_CANDIDATE_EVIDENCE_FILE_BYTES", max(sizes) - (defect == "per-file")
    )
    monkeypatch.setattr(
        review_loop_orchestrator, "MAX_CANDIDATE_EVIDENCE_TOTAL_BYTES", sum(sizes) - (defect == "aggregate")
    )
    if defect == "symlink":
        external = tmp_path / "synthetic.json"
        paths[0].rename(external)
        paths[0].symlink_to(external)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    creator = FakeCreatorRunner(layout=store.layout, replies=[{"done": True, "candidate": {"content": "reviewed"}}])
    reviewer = FakeReviewerRunner(layout=store.layout, reviewer="synthetic", replies=[{"verdict": "ok"}])

    if defect == "at-limit":
        state = run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
        )
        assert state.decision is not None and state.decision.outcome == LoopOutcome.converged
        return

    with pytest.raises(LoopProtocolError, match=r"repair or archive copy/candidates/description/"):
        run_review_loop_orchestrator(
            workspace=tmp_path, asset_id="description", max_iterations=3, creator=creator, reviewer=reviewer
        )

    assert not creator.calls and not reviewer.calls
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert paths[0].is_symlink() == (defect == "symlink")
