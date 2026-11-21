from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock

import pytest

from podcast_pipeline import agent_runners
from podcast_pipeline.agent_cli_config import AgentCliConfig
from podcast_pipeline.agent_runners import ClaudeCodeReviewerRunner, load_episode_context_from_workspace
from podcast_pipeline.domain.models import Candidate, ProvenanceRef
from podcast_pipeline.prompting import (
    PromptRegistry,
    PromptRenderer,
    PromptRenderResult,
    PromptStore,
    PromptTemplate,
    default_prompt_registry,
    render_asset_candidates_prompt,
    render_chunk_summary_prompt,
    render_creator_prompt,
    render_episode_context,
    render_episode_summary_prompt,
    render_reviewer_prompt,
)
from podcast_pipeline.review_loop_engine import CreatorInput, ReviewerInput
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore


def test_prompt_registry_rejects_duplicate_names() -> None:
    first = PromptTemplate(name="simple", template="Hello")
    distinct = PromptTemplate(name="other", template="Hi")
    second = PromptTemplate(name="simple", template="Goodbye")

    registry = PromptRegistry([first, distinct])
    assert registry.get("simple") is first
    assert registry.get("other") is distinct
    with pytest.raises(ValueError, match=r"^Duplicate prompt template name: 'simple'$"):
        PromptRegistry([first, distinct, second])


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


@pytest.mark.parametrize(
    ("context", "glossary", "field"),
    [
        ({"name": "Pod"}, {"Mic": "\ud800"}, "glossary definition"),
        ({"name": "Pod", "unused": "\udfff"}, {}, "context value"),
        ({"name": "Pod", "\ud800": "unused"}, {}, "context key"),
    ],
)
def test_prompt_renderer_refuses_surrogates_before_receipt(
    monkeypatch: pytest.MonkeyPatch,
    context: dict[str, str],
    glossary: dict[str, str],
    field: str,
) -> None:
    renderer = PromptRenderer(PromptRegistry([PromptTemplate(name="simple", template="Hello {name}")]))
    receipt = Mock(wraps=PromptRenderResult)
    monkeypatch.setattr("podcast_pipeline.prompting.PromptRenderResult", receipt)

    with pytest.raises(ValueError, match=rf"{field}.*Unicode scalar text"):
        renderer.render(name="simple", context=context, glossary=glossary)

    receipt.assert_not_called()


def test_direct_prompt_receipt_refuses_surrogate() -> None:
    with pytest.raises(ValueError, match="prompt text.*Unicode scalar text"):
        PromptRenderResult(template="simple", text="\ud800", context={}, glossary=(), few_shots=())


def test_prompt_receipt_preserves_non_bmp_identity() -> None:
    renderer = PromptRenderer(PromptRegistry([PromptTemplate(name="simple", template="Hello {name}")]))
    rendered = renderer.render(
        name="simple",
        context={"name": "🎙", "unused": "🛰"},
        glossary={"Mic": "🎤"},
        few_shots=[{"input": "🗣", "output": "👋"}],
    )

    assert rendered.text.encode("utf-8").decode("utf-8").startswith("Hello 🎙")
    assert rendered.prompt_id == "simple_f6d2a4c62785051f4cfdbbe884e8d27e09649241473be1ea77149b5f430e79bd"


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


def test_prompt_receipts_keep_distinct_contexts_and_legacy_reference(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    prompt_store = PromptStore(store)
    renderer = PromptRenderer(PromptRegistry([PromptTemplate(name="simple", template="Hello {name}")]))
    first = renderer.render(name="simple", context={"name": "Pod", "unused": "first"})
    second = renderer.render(name="simple", context={"name": "Pod", "unused": "second"})
    assert first.text == second.text

    legacy_digest = sha256(f"simple\n{first.text}".encode()).hexdigest()[:12]
    legacy_ref = ProvenanceRef(kind="prompts", ref=f"simple_{legacy_digest}")
    legacy_path = store.write_provenance_json(legacy_ref, {**first.to_json_data(), "prompt_id": legacy_ref.ref})
    legacy_bytes = legacy_path.read_bytes()

    first_ref = prompt_store.write(first)
    second_ref = prompt_store.write(second)
    assert first_ref != second_ref
    for rendered, ref in [(first, first_ref), (second, second_ref)]:
        path = store.layout.provenance_json_path(ref.kind, ref.ref)
        receipt = json.loads(path.read_text(encoding="utf-8"))
        assert receipt["context"] == rendered.context
        assert receipt["prompt_id"] == ref.ref
    assert legacy_path.read_bytes() == legacy_bytes

    same = renderer.render(name="simple", context={"unused": "first", "name": "Pod"})
    assert prompt_store.write(same) == first_ref
    assert len(list(legacy_path.parent.glob("*.json"))) == 3


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

    monkeypatch.setattr(agent_runners, "run_cli_process", fake_run)

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


def test_render_episode_context_with_hosts() -> None:
    ctx = render_episode_context(
        summary="A test episode.",
        hosts=["Jochen", "Dominik"],
    )
    assert "Jochen, Dominik" in ctx
    assert "do not invent" in ctx
    assert "Episode summary:" in ctx


def test_render_episode_context_without_hosts() -> None:
    ctx = render_episode_context(summary="A test episode.")
    assert "hosts" not in ctx.lower() or "do not invent" not in ctx


def test_render_episode_context_empty_returns_empty_string() -> None:
    assert render_episode_context() == ""
    assert render_episode_context(summary=None, chapters=None) == ""
    assert render_episode_context(summary="", chapters="") == ""


def test_render_episode_context_truncates_long_transcript() -> None:
    long_text = "x" * 5000
    ctx = render_episode_context(transcript_excerpt=long_text, max_transcript_chars=100)
    assert "[...truncated]" in ctx
    assert len(ctx) < 5000


def test_render_episode_context_admits_exact_limits_and_clips_transcript() -> None:
    points = ["p"] * 199
    chapters = "c"
    summary = "s" * (65536 - len(points) - len(chapters))
    context = render_episode_context(
        summary=summary,
        key_points=points,
        chapters=chapters,
        transcript_excerpt="hello world",
        max_transcript_chars=5,
    )
    assert summary in context
    assert context.endswith("Transcript excerpt:\nhello\n[...truncated]")


def test_render_episode_context_counts_chapter_lines_with_other_items() -> None:
    chapters = "Opening\n" * 200
    with pytest.raises(ValueError, match="200 items"):
        render_episode_context(chapters=chapters, key_points=["A point"], transcript_excerpt="A normal transcript.")


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


def test_load_episode_context_from_workspace_with_hosts(tmp_path: Path) -> None:
    import yaml

    layout = EpisodeWorkspaceLayout(root=tmp_path)

    episode_data = {
        "episode_id": "test_ep",
        "hosts": ["Jochen", "Dominik"],
    }
    (tmp_path / "episode.yaml").write_text(yaml.safe_dump(episode_data), encoding="utf-8")

    # Create a summary so context is not None
    summary_dir = layout.episode_summary_dir
    summary_dir.mkdir(parents=True)
    summary_data = {
        "version": 1,
        "summary_markdown": "A test episode.",
        "key_points": ["key"],
    }
    layout.episode_summary_json_path().write_text(json.dumps(summary_data), encoding="utf-8")

    ctx = load_episode_context_from_workspace(layout)
    assert ctx is not None
    assert "Jochen, Dominik" in ctx
    assert "do not invent" in ctx


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


def test_render_asset_candidates_prompt_with_hosts() -> None:
    renderer = PromptRenderer(default_prompt_registry())

    result = render_asset_candidates_prompt(
        renderer=renderer,
        asset_id="description",
        asset_guidance="Write a description.",
        episode_summary_markdown="# Summary\n",
        key_points=["point A"],
        topics=["topic X"],
        chapters=["00:00 Intro"],
        num_candidates=3,
        hosts=["Jochen", "Dominik"],
    )

    assert "Jochen, Dominik" in result.text
    assert "do not invent" in result.text


def test_render_asset_candidates_prompt_without_hosts() -> None:
    renderer = PromptRenderer(default_prompt_registry())

    result = render_asset_candidates_prompt(
        renderer=renderer,
        asset_id="description",
        asset_guidance="Write a description.",
        episode_summary_markdown="# Summary\n",
        key_points=["point A"],
        topics=["topic X"],
        chapters=["00:00 Intro"],
        num_candidates=3,
    )

    assert "do not invent" not in result.text


def test_render_chunk_summary_prompt_with_hosts() -> None:
    renderer = PromptRenderer(default_prompt_registry())

    result = render_chunk_summary_prompt(
        renderer=renderer,
        chunk_id=1,
        chunk_text="Speaker: Hallo und willkommen.",
        hosts=["Jochen", "Dominik"],
    )

    assert "Jochen, Dominik" in result.text
    assert "do not invent" in result.text


def test_render_chunk_summary_prompt_without_hosts() -> None:
    renderer = PromptRenderer(default_prompt_registry())

    result = render_chunk_summary_prompt(
        renderer=renderer,
        chunk_id=1,
        chunk_text="Speaker: Hallo und willkommen.",
    )

    assert "do not invent" not in result.text


def test_render_episode_summary_prompt_with_hosts() -> None:
    renderer = PromptRenderer(default_prompt_registry())

    result = render_episode_summary_prompt(
        renderer=renderer,
        chunk_summaries_json='[{"summary_markdown": "test"}]',
        hosts=["Jochen", "Dominik"],
    )

    assert "Jochen, Dominik" in result.text
    assert "do not invent" in result.text


def test_render_episode_summary_prompt_without_hosts() -> None:
    renderer = PromptRenderer(default_prompt_registry())

    result = render_episode_summary_prompt(
        renderer=renderer,
        chunk_summaries_json='[{"summary_markdown": "test"}]',
    )

    assert "do not invent" not in result.text


def test_render_asset_candidates_prompt_includes_envelope_schema() -> None:
    renderer = PromptRenderer(default_prompt_registry())

    result = render_asset_candidates_prompt(
        renderer=renderer,
        asset_id="description",
        asset_guidance="Write a description.",
        episode_summary_markdown="# Summary\n",
        key_points=["point A"],
        topics=["topic X"],
        chapters=["00:00 Intro"],
        num_candidates=3,
    )

    # Verify via parsed schema structure (not literal text) to avoid formatting brittleness
    parsed_schema = json.loads(result.context["response_schema"])
    assert parsed_schema["type"] == "object"
    assert parsed_schema["required"] == ["candidates"]
    assert parsed_schema["additionalProperties"] is False

    candidates_prop = parsed_schema["properties"]["candidates"]
    assert candidates_prop["type"] == "array"
    assert "items" in candidates_prop
    # Count constraints should be embedded
    assert candidates_prop["minItems"] == 3
    assert candidates_prop["maxItems"] == 3
