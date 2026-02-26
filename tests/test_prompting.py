from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from podcast_pipeline.agent_cli_config import AgentCliConfig
from podcast_pipeline.agent_runners import ClaudeCodeReviewerRunner, load_episode_context_from_workspace
from podcast_pipeline.domain.models import Candidate
from podcast_pipeline.prompting import (
    PromptRegistry,
    PromptRenderer,
    PromptStore,
    PromptTemplate,
    default_prompt_registry,
    render_creator_prompt,
    render_episode_context,
    render_reviewer_prompt,
)
from podcast_pipeline.review_loop_engine import CreatorInput, ReviewerInput
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore


def test_prompt_renderer_injects_glossary_and_few_shot_deterministically() -> None:
    registry = PromptRegistry([PromptTemplate(name="simple", template="Hello {name}")])
    renderer = PromptRenderer(registry)

    glossary = {"LLM": "language model", "AI": "artificial intelligence"}
    few_shots = [{"input": "Ping", "output": "Pong"}]

    rendered_a = renderer.render(
        name="simple",
        context={"name": "Pod"},
        glossary=glossary,
        few_shots=few_shots,
    )
    rendered_b = renderer.render(
        name="simple",
        context={"name": "Pod"},
        glossary=glossary,
        few_shots=few_shots,
    )

    assert rendered_a.text == rendered_b.text
    assert "Glossary:\n- AI: artificial intelligence\n- LLM: language model" in rendered_a.text
    assert "Few-shot examples:" in rendered_a.text
    assert "User:\nPing" in rendered_a.text
    assert "Assistant:\nPong" in rendered_a.text


def test_prompt_store_writes_prompt_under_provenance(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    renderer = PromptRenderer(default_prompt_registry())

    inp = ReviewerInput(
        asset_id="description",
        iteration=1,
        candidate=Candidate(asset_id="description", content="draft"),
    )
    rendered = render_reviewer_prompt(renderer=renderer, inp=inp)

    prompt_store = PromptStore(store)
    provenance = prompt_store.write(rendered)

    path = store.layout.provenance_json_path("prompts", provenance.ref)
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["prompt_id"] == rendered.prompt_id
    assert payload["template"] == rendered.template
    assert payload["prompt_text"] == rendered.text


def test_reviewer_runner_attaches_prompt_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    renderer = PromptRenderer(default_prompt_registry())
    inp = ReviewerInput(
        asset_id="description",
        iteration=1,
        candidate=Candidate(asset_id="description", content="draft"),
    )
    prompt = render_reviewer_prompt(renderer=renderer, inp=inp)

    config = AgentCliConfig(role="reviewer", command="claude", args=())
    output = json.dumps({"verdict": "ok"})

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    runner = ClaudeCodeReviewerRunner(layout=layout, config=config)
    review = runner.run_with_prompt(prompt=prompt, asset_id="description", iteration=1)

    review_path = layout.review_iteration_json_path("description", 1, reviewer="reviewer")
    saved = json.loads(review_path.read_text(encoding="utf-8"))
    assert saved["verdict"] == "ok"
    assert saved["provenance"][0]["kind"] == "prompts"
    assert saved["provenance"][0]["ref"] == prompt.prompt_id

    prompt_path = layout.provenance_json_path("prompts", prompt.prompt_id)
    assert prompt_path.exists()
    assert review.provenance[0].ref == prompt.prompt_id


def test_render_episode_context_with_all_fields() -> None:
    ctx = render_episode_context(
        summary="This episode covers AI tools.",
        key_points=["LLMs are useful", "Agents automate tasks"],
        chapters="00:00 Intro\n05:00 Main topic",
        transcript_excerpt="Speaker 1: Hello world.",
    )
    assert "Episode summary:" in ctx
    assert "This episode covers AI tools." in ctx
    assert "Key points:" in ctx
    assert "- LLMs are useful" in ctx
    assert "- Agents automate tasks" in ctx
    assert "Chapters:" in ctx
    assert "00:00 Intro" in ctx
    assert "Transcript excerpt:" in ctx
    assert "Speaker 1: Hello world." in ctx


def test_render_episode_context_empty_returns_empty_string() -> None:
    assert render_episode_context() == ""
    assert render_episode_context(summary=None, chapters=None) == ""
    assert render_episode_context(summary="", chapters="") == ""


def test_render_episode_context_truncates_long_transcript() -> None:
    long_text = "x" * 5000
    ctx = render_episode_context(transcript_excerpt=long_text, max_transcript_chars=100)
    assert "[...truncated]" in ctx
    assert len(ctx) < 5000


def test_creator_prompt_includes_episode_context() -> None:
    renderer = PromptRenderer(default_prompt_registry())
    inp = CreatorInput(asset_id="description", iteration=1, previous_candidate=None, previous_review=None)
    rendered = render_creator_prompt(
        renderer=renderer,
        inp=inp,
        episode_context="Episode summary:\nA great podcast about tech.",
    )
    assert "Episode summary:" in rendered.text
    assert "A great podcast about tech." in rendered.text


def test_reviewer_prompt_includes_episode_context() -> None:
    renderer = PromptRenderer(default_prompt_registry())
    inp = ReviewerInput(
        asset_id="description",
        iteration=1,
        candidate=Candidate(asset_id="description", content="draft"),
    )
    rendered = render_reviewer_prompt(
        renderer=renderer,
        inp=inp,
        episode_context="Chapters:\n00:00 Intro\n05:00 Discussion",
    )
    assert "Chapters:" in rendered.text
    assert "00:00 Intro" in rendered.text


def test_creator_prompt_without_episode_context_still_works() -> None:
    renderer = PromptRenderer(default_prompt_registry())
    inp = CreatorInput(asset_id="description", iteration=1, previous_candidate=None, previous_review=None)
    rendered = render_creator_prompt(renderer=renderer, inp=inp)
    assert "Creator agent" in rendered.text
    assert "no markdown fencing" in rendered.text


def test_load_episode_context_from_workspace_with_files(tmp_path: Path) -> None:
    import yaml

    layout = EpisodeWorkspaceLayout(root=tmp_path)

    # Create transcript and chapters
    transcript_dir = tmp_path / "transcript"
    transcript_dir.mkdir()
    (transcript_dir / "transcript.txt").write_text("Speaker 1: Hello.\n", encoding="utf-8")
    (transcript_dir / "chapters.txt").write_text("00:00 Intro\n", encoding="utf-8")

    # Create episode.yaml
    episode_data = {
        "episode_id": "test_ep",
        "inputs": {
            "transcript": "transcript/transcript.txt",
            "chapters": "transcript/chapters.txt",
        },
    }
    (tmp_path / "episode.yaml").write_text(yaml.safe_dump(episode_data), encoding="utf-8")

    # Create episode summary
    summary_dir = layout.episode_summary_dir
    summary_dir.mkdir(parents=True)
    summary_data = {
        "version": 1,
        "summary_markdown": "A test episode about greetings.",
        "key_points": ["Saying hello", "Being friendly"],
    }
    layout.episode_summary_json_path().write_text(json.dumps(summary_data), encoding="utf-8")

    ctx = load_episode_context_from_workspace(layout)
    assert ctx is not None
    assert "A test episode about greetings." in ctx
    assert "- Saying hello" in ctx
    assert "00:00 Intro" in ctx
    assert "Speaker 1: Hello." in ctx


def test_load_episode_context_from_workspace_empty(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    ctx = load_episode_context_from_workspace(layout)
    assert ctx is None


def test_load_episode_context_summary_wrong_shape(tmp_path: Path) -> None:
    """episode_summary.json is valid JSON but a list instead of a dict."""
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    summary_dir = layout.episode_summary_dir
    summary_dir.mkdir(parents=True)
    layout.episode_summary_json_path().write_text("[]", encoding="utf-8")

    ctx = load_episode_context_from_workspace(layout)
    assert ctx is None


def test_load_episode_context_summary_non_string_markdown(tmp_path: Path) -> None:
    """summary_markdown is a number instead of a string."""
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    summary_dir = layout.episode_summary_dir
    summary_dir.mkdir(parents=True)
    layout.episode_summary_json_path().write_text(
        json.dumps({"summary_markdown": 123, "key_points": ["ok"]}),
        encoding="utf-8",
    )

    ctx = load_episode_context_from_workspace(layout)
    # summary_markdown is skipped but key_points still load
    assert ctx is not None
    assert "- ok" in ctx
    assert "123" not in ctx


def test_load_episode_context_missing_referenced_transcript(tmp_path: Path) -> None:
    """episode.yaml references a transcript file that doesn't exist."""
    import yaml

    layout = EpisodeWorkspaceLayout(root=tmp_path)
    episode_data = {
        "episode_id": "test_ep",
        "inputs": {"transcript": "nonexistent/transcript.txt"},
    }
    (tmp_path / "episode.yaml").write_text(yaml.safe_dump(episode_data), encoding="utf-8")

    ctx = load_episode_context_from_workspace(layout)
    assert ctx is None


def test_load_episode_context_non_utf8_transcript(tmp_path: Path) -> None:
    """Transcript file contains non-UTF-8 bytes — should degrade gracefully."""
    import yaml

    layout = EpisodeWorkspaceLayout(root=tmp_path)
    transcript_dir = tmp_path / "transcript"
    transcript_dir.mkdir()
    # Write raw latin-1 bytes that are invalid UTF-8
    (transcript_dir / "transcript.txt").write_bytes(b"\xff\xfe bad encoding")

    episode_data = {
        "episode_id": "test_ep",
        "inputs": {"transcript": "transcript/transcript.txt"},
    }
    (tmp_path / "episode.yaml").write_text(yaml.safe_dump(episode_data), encoding="utf-8")

    ctx = load_episode_context_from_workspace(layout)
    # Transcript can't be read, so context is None (no other sources)
    assert ctx is None
