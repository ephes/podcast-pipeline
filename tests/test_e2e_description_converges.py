from __future__ import annotations

import json
from pathlib import Path

from podcast_pipeline.agent_runners import FakeCreatorRunner, FakeReviewerRunner
from podcast_pipeline.domain.models import ReviewVerdict
from podcast_pipeline.review_loop_engine import LoopOutcome, ProtocolWrite, run_review_loop_engine
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore


def _fixture_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "pp_068"


def _load_fixture_text(name: str) -> str:
    return (_fixture_dir() / name).read_text(encoding="utf-8")


def _write_protocol_files(writes: tuple[ProtocolWrite, ...]) -> None:
    for write in writes:
        write.path.parent.mkdir(parents=True, exist_ok=True)
        write.path.write_text(write.dumps(), encoding="utf-8")


def _first_non_empty_line(raw: str) -> str:
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    raise AssertionError("expected at least one non-empty line")


def test_e2e_description_asset_converges_and_writes_artifacts(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    store = EpisodeWorkspaceStore(tmp_path)

    transcript_raw = _load_fixture_text("transcript.txt")
    chapters_raw = _load_fixture_text("chapters.txt")

    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    (inputs_dir / "transcript.txt").write_text(transcript_raw, encoding="utf-8")
    (inputs_dir / "chapters.txt").write_text(chapters_raw, encoding="utf-8")

    first_chapter = _first_non_empty_line(chapters_raw)
    first_transcript_line = _first_non_empty_line(transcript_raw)
    initial_description = "\n".join(
        [
            "# Episode description",
            "",
            "## Chapters",
            first_chapter,
            "",
            "## Transcript excerpt",
            first_transcript_line,
            "",
        ]
    )

    creator = FakeCreatorRunner(
        layout=layout,
        replies=[
            {"done": False, "candidate": {"content": initial_description}},
            {"done": True, "candidate": {"content": initial_description + "\nRevision 2\n"}},
        ],
    )
    reviewer = FakeReviewerRunner(
        layout=layout,
        reviewer="reviewer_a",
        replies=[
            {"verdict": "changes_requested", "issues": [{"message": "add more detail"}]},
            {"verdict": "ok"},
        ],
    )

    protocol_state, protocol_writes = run_review_loop_engine(
        layout=layout,
        asset_id="description",
        max_iterations=3,
        creator=creator,
        reviewer=reviewer,
    )

    for it in protocol_state.iterations:
        store.write_candidate(it.candidate)
        store.write_review("description", it.review)

    final_iteration = protocol_state.iterations[-1]
    store.write_selected_text("description", final_iteration.candidate.format, final_iteration.candidate.content)
    _write_protocol_files(protocol_writes)

    assert protocol_state.decision is not None
    assert protocol_state.decision.outcome == LoopOutcome.converged
    assert final_iteration.review.verdict == ReviewVerdict.ok

    for it in protocol_state.iterations:
        assert layout.candidate_json_path("description", it.candidate.candidate_id).exists()
        assert layout.review_iteration_json_path("description", it.iteration, reviewer="reviewer_a").exists()
        assert layout.protocol_iteration_json_path("description", it.iteration).exists()

        payload = json.loads(
            layout.protocol_iteration_json_path("description", it.iteration).read_text(encoding="utf-8")
        )
        assert payload["reviewer"]["verdict"] in {"changes_requested", "ok"}

    assert layout.protocol_state_json_path("description").exists()
    assert layout.selected_text_path("description", final_iteration.candidate.format).exists()
