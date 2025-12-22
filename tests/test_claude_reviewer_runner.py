from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from podcast_pipeline.agent_cli_config import AgentCliConfig
from podcast_pipeline.agent_runners import ClaudeCodeReviewerRunner
from podcast_pipeline.domain.models import ReviewVerdict
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


def test_claude_reviewer_runner_writes_review_from_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("Review me", encoding="utf-8")

    config = AgentCliConfig(
        role="reviewer",
        command="claude",
        args=("--format", "json"),
    )
    output = json.dumps(
        {
            "verdict": "changes_requested",
            "issues": [{"message": "Fix the intro"}],
        },
    )

    calls: list[dict[str, object]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    runner = ClaudeCodeReviewerRunner(layout=layout, config=config)
    review = runner.run_prompt(prompt_path=prompt_path, asset_id="description", iteration=1)

    assert review.verdict == ReviewVerdict.changes_requested
    review_path = layout.review_iteration_json_path("description", 1, reviewer="reviewer")
    saved = json.loads(review_path.read_text(encoding="utf-8"))
    assert saved["verdict"] == "changes_requested"
    assert saved["iteration"] == 1
    assert saved["reviewer"] == "reviewer"
    assert saved["issues"][0]["message"] == "Fix the intro"

    assert len(calls) == 1
    call = calls[0]
    assert call["args"] == ["claude", "--format", "json"]
    assert call["input"] == "Review me"
    assert call["text"] is True
    assert call["capture_output"] is True
    assert call["check"] is False
    assert call["cwd"] == str(tmp_path)


def test_claude_reviewer_runner_accepts_wrapped_review_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("Prompt", encoding="utf-8")

    config = AgentCliConfig(
        role="reviewer",
        command="claude",
        args=(),
    )
    output = json.dumps({"review": {"verdict": "ok", "iteration": 9}})

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    runner = ClaudeCodeReviewerRunner(layout=layout, config=config)
    review = runner.run_prompt(prompt_path=prompt_path, asset_id="description", iteration=2)

    assert review.verdict == ReviewVerdict.ok
    assert review.iteration == 2
    review_path = layout.review_iteration_json_path("description", 2, reviewer="reviewer")
    saved = json.loads(review_path.read_text(encoding="utf-8"))
    assert saved["iteration"] == 2
