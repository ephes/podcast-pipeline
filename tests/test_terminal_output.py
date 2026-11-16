from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from podcast_pipeline.domain.models import Candidate, TextFormat
from podcast_pipeline.entrypoints.cli import app
from podcast_pipeline.entrypoints.pick import _candidate_preview

HOSTILE = "Synthetic\x1b]52;c;payload\x07\rRewrite\x9b2J"


def test_pick_preview_escapes_controls_without_changing_candidate() -> None:
    candidate = Candidate(asset_id="description", format=TextFormat.markdown, content=HOSTILE + "x" * 10000)
    before = candidate.model_dump_json()
    preview = _candidate_preview(candidate)
    assert "\x1b" not in preview
    assert "\\x1b" in preview
    assert "\\r" in preview
    assert "\\x9b" in preview
    assert len(preview) <= 72
    assert candidate.model_dump_json() == before


def test_status_escapes_review_issue_controls(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runner = CliRunner()
    assert runner.invoke(app, ["review", "--fake-runner", "--workspace", str(workspace)]).exit_code == 0
    path = workspace / "copy/protocol/description/state.json"
    raw = json.loads(path.read_text())
    raw["iterations"][-1]["reviewer"]["issues"] = [{"severity": "warning", "message": HOSTILE + "x" * 10000}]
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    result = runner.invoke(app, ["status", "--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output
    assert "\x9b" not in result.output
    assert "\\x1b" in result.output
    assert "\\r" in result.output
    assert max(map(len, result.output.splitlines())) <= 1000
    assert path.read_bytes() == before
