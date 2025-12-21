from __future__ import annotations

import re
from pathlib import Path

import typer

from podcast_pipeline.agent_runners import FakeCreatorRunner, FakeReviewerRunner
from podcast_pipeline.domain.models import EpisodeWorkspace
from podcast_pipeline.review_loop_orchestrator import run_review_loop_orchestrator
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore

_ASSET_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _first_non_empty_line(raw: str) -> str:
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _pick_demo_workspace_dir(base: Path) -> Path:
    if not base.exists():
        return base
    for idx in range(2, 1_000):
        candidate = base.with_name(f"{base.name}_{idx}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find free demo workspace name for base: {base}")


def _default_transcript() -> str:
    return "\n".join(
        [
            "Speaker 1: Welcome to the podcast pipeline demo.",
            "Speaker 2: Today we explore a tiny Creator/Reviewer loop.",
            "",
        ],
    )


def _default_chapters() -> str:
    return "\n".join(
        [
            "00:00 Intro",
            "05:00 Main topic",
            "",
        ],
    )


def _build_initial_description(*, transcript: str, chapters: str) -> str:
    first_chapter = _first_non_empty_line(chapters)
    first_transcript_line = _first_non_empty_line(transcript)
    return "\n".join(
        [
            "# Episode description",
            "",
            "## Chapters",
            first_chapter or "(no chapters)",
            "",
            "## Transcript excerpt",
            first_transcript_line or "(no transcript)",
            "",
        ],
    )


def run_draft_demo(
    *,
    fake_runner: bool,
    workspace: Path | None,
    episode_id: str,
    asset_id: str,
    max_iterations: int,
) -> None:
    if not fake_runner:
        typer.echo("Only `podcast draft --fake-runner` is implemented right now.", err=True)
        raise typer.Exit(code=2)

    if not _ASSET_ID_RE.fullmatch(asset_id):
        raise typer.BadParameter("asset_id must match ^[a-z][a-z0-9_]*$")

    if workspace is None:
        root = _pick_demo_workspace_dir(Path.cwd() / "demo_workspace")
    else:
        root = workspace
        if root.exists():
            raise typer.BadParameter(f"workspace already exists: {root}")

    root.mkdir(parents=True, exist_ok=False)

    store = EpisodeWorkspaceStore(root)
    transcript_dir = root / "transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)

    transcript_path = transcript_dir / "transcript.txt"
    chapters_path = transcript_dir / "chapters.txt"
    transcript_path.write_text(_default_transcript(), encoding="utf-8")
    chapters_path.write_text(_default_chapters(), encoding="utf-8")

    store.write_episode_yaml(
        {
            "episode_id": episode_id,
            "inputs": {
                "transcript": str(transcript_path.relative_to(root)),
                "chapters": str(chapters_path.relative_to(root)),
            },
        },
    )
    store.write_state(EpisodeWorkspace(episode_id=episode_id, root_dir=str(root)))

    initial_description = _build_initial_description(
        transcript=transcript_path.read_text(encoding="utf-8"),
        chapters=chapters_path.read_text(encoding="utf-8"),
    )

    creator = FakeCreatorRunner(
        layout=store.layout,
        replies=[
            {"done": False, "candidate": {"content": initial_description}},
            {"done": True, "candidate": {"content": initial_description + "\nRevision 2\n"}},
        ],
    )
    reviewer = FakeReviewerRunner(
        layout=store.layout,
        reviewer="reviewer_a",
        replies=[
            {"verdict": "changes_requested", "issues": [{"message": "add more detail"}]},
            {"verdict": "ok"},
        ],
    )

    protocol_state = run_review_loop_orchestrator(
        workspace=store.layout.root,
        asset_id=asset_id,
        max_iterations=max_iterations,
        creator=creator,
        reviewer=reviewer,
    )
    typer.echo(f"Workspace: {root}")
    if protocol_state.iterations:
        final_iteration = protocol_state.iterations[-1]
        typer.echo(f"Selected: {store.layout.selected_text_path(asset_id, final_iteration.candidate.format)}")
