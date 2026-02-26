from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import typer

from podcast_pipeline.agent_cli_config import collect_agent_cli_issues, load_agent_cli_bundle
from podcast_pipeline.agent_runners import (
    FakeCreatorRunner,
    FakeReviewerRunner,
    build_local_cli_runners,
    load_episode_context_from_workspace,
)
from podcast_pipeline.domain.models import EpisodeWorkspace, ReviewIteration
from podcast_pipeline.review_loop_engine import CreatorInput, CreatorOutput, LoopOutcome, ReviewerInput
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


def _open_existing_workspace(root: Path) -> EpisodeWorkspaceStore:
    """Open an existing workspace directory, validating that episode.yaml exists."""
    store = EpisodeWorkspaceStore(root)
    if not store.layout.episode_yaml.exists():
        raise typer.BadParameter(f"workspace exists but has no episode.yaml: {root}")
    return store


def _create_demo_workspace(root: Path, episode_id: str) -> EpisodeWorkspaceStore:
    """Create a new demo workspace with stub transcript and chapters."""
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
    return store


def _read_input_text(store: EpisodeWorkspaceStore, key: str) -> str:
    """Read a text file referenced by key in episode.yaml inputs."""
    episode_data = store.read_episode_yaml()
    inputs = episode_data.get("inputs", {})
    rel = inputs.get(key)
    if isinstance(rel, str):
        path = store.layout.root / rel
        if path.exists():
            return path.read_text(encoding="utf-8")
    return ""


def run_draft_demo(
    *,
    fake_runner: bool,
    workspace: Path | None,
    episode_id: str,
    asset_id: str,
    max_iterations: int,
) -> None:
    if not fake_runner:
        issues = collect_agent_cli_issues(workspace=workspace, roles=("creator", "reviewer"))
        for issue in issues:
            typer.echo(issue, err=True)
        if issues:
            raise typer.Exit(code=2)

    if not _ASSET_ID_RE.fullmatch(asset_id):
        raise typer.BadParameter("asset_id must match ^[a-z][a-z0-9_]*$")

    existing_workspace = workspace is not None and workspace.exists()

    if existing_workspace:
        assert workspace is not None
        root = workspace
        store = _open_existing_workspace(root)
    else:
        root = workspace if workspace is not None else _pick_demo_workspace_dir(Path.cwd() / "demo_workspace")
        store = _create_demo_workspace(root, episode_id)

    creator: Callable[[CreatorInput], CreatorOutput]
    reviewer: Callable[[ReviewerInput], ReviewIteration]
    if fake_runner:
        if existing_workspace:
            transcript_text = _read_input_text(store, "transcript")
            chapters_text = _read_input_text(store, "chapters")
        else:
            transcript_text = _default_transcript()
            chapters_text = _default_chapters()
        initial_description = _build_initial_description(transcript=transcript_text, chapters=chapters_text)

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
    else:
        episode_context = load_episode_context_from_workspace(store.layout)
        bundle = load_agent_cli_bundle(workspace=store.layout.root)
        creator, reviewer = build_local_cli_runners(
            layout=store.layout,
            bundle=bundle,
            episode_context=episode_context,
        )

    protocol_state = run_review_loop_orchestrator(
        workspace=store.layout.root,
        asset_id=asset_id,
        max_iterations=max_iterations,
        creator=creator,
        reviewer=reviewer,
    )
    typer.echo(f"Workspace: {root}")
    decision = protocol_state.decision
    if decision is not None and decision.outcome == LoopOutcome.converged and protocol_state.iterations:
        final_iteration = protocol_state.iterations[-1]
        typer.echo(f"Selected: {store.layout.selected_text_path(asset_id, final_iteration.candidate.format)}")
    elif decision is not None:
        typer.echo(f"Outcome: {decision.outcome.value} ({decision.reason})")
        typer.echo("Run `podcast pick` to manually select a candidate.")
