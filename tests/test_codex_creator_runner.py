from __future__ import annotations

import json
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from podcast_pipeline.agent_cli_config import AgentCliConfig
from podcast_pipeline.agent_runners import CodexCliCreatorRunner
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


def test_codex_creator_runner_writes_creator_iteration_and_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("Update description", encoding="utf-8")

    config = AgentCliConfig(
        role="creator",
        command="codex",
        args=("--format", "json"),
    )
    candidate_id = "01234567-89ab-cdef-0123-456789abcdef"
    created_at = "2025-01-02T03:04:05+00:00"
    output = json.dumps(
        {
            "applied": True,
            "done": False,
            "candidate": {
                "asset_id": "description",
                "content": "# Draft\n",
                "format": "markdown",
                "candidate_id": candidate_id,
                "created_at": created_at,
            },
        },
    )

    calls: list[dict[str, object]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    runner = CodexCliCreatorRunner(layout=layout, config=config)
    out = runner.run_prompt(prompt_path=prompt_path, asset_id="description", iteration=2)

    assert out.done is False
    assert out.candidate.asset_id == "description"
    assert out.candidate.candidate_id == UUID(candidate_id)

    candidate_path = layout.candidate_json_path("description", UUID(candidate_id))
    saved_candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert saved_candidate["content"] == "# Draft\n"
    assert saved_candidate["candidate_id"] == candidate_id
    assert layout.candidate_text_path("description", UUID(candidate_id), out.candidate.format).exists()

    creator_path = layout.creator_iteration_json_path("description", 2)
    saved_creator = json.loads(creator_path.read_text(encoding="utf-8"))
    assert saved_creator["applied"] is True
    assert saved_creator["done"] is False
    assert saved_creator["candidate_id"] == candidate_id

    assert len(calls) == 1
    call = calls[0]
    assert call["args"] == ["codex", "--format", "json"]
    assert call["input"] == "Update description"
    assert call["text"] is True
    assert call["capture_output"] is True
    assert call["check"] is False
    assert call["cwd"] == str(tmp_path)
