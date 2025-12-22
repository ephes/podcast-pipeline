from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from podcast_pipeline.agent_cli_config import AgentCliConfig
from podcast_pipeline.agent_runners import ClaudeCodeReviewerRunner
from podcast_pipeline.domain.models import Candidate
from podcast_pipeline.prompting import (
    PromptRegistry,
    PromptRenderer,
    PromptStore,
    PromptTemplate,
    default_prompt_registry,
    render_reviewer_prompt,
)
from podcast_pipeline.review_loop_engine import ReviewerInput
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
