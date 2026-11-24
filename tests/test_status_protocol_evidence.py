from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from podcast_pipeline.entrypoints.cli import app


@pytest.fixture
def protocol(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    result = CliRunner().invoke(app, ["review", "--fake-runner", "--workspace", str(tmp_path / "workspace")])
    assert result.exit_code == 0, result.output
    path = tmp_path / "workspace/copy/protocol/description/state.json"
    return path, json.loads(path.read_text())


@pytest.mark.parametrize("duplicate", [False, True])
def test_status_rejects_protocol_directory_impersonation(
    tmp_path: Path, protocol: tuple[Path, dict[str, Any]], duplicate: bool
) -> None:
    path, raw = protocol
    other = tmp_path / "workspace/copy/protocol/title_detail/state.json"
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps(raw))
    if not duplicate:
        path.unlink()
    result = CliRunner().invoke(app, ["status", "--workspace", str(tmp_path / "workspace")])
    assert result.exit_code != 0
    assert "invalid" in result.output.lower()
    assert "Outcome: converged" not in result.output


@pytest.mark.parametrize(
    "damage",
    [
        "empty",
        "missing_final",
        "bool_limit",
        "bool_final",
        "bool_iteration",
        "bool_review",
        "gap",
        "duplicate",
        "blocking",
        "not_done",
        "unknown_outcome",
        "after_terminal",
        "duplicate_member",
    ],
)
def test_status_requires_matching_terminal_review(
    tmp_path: Path, protocol: tuple[Path, dict[str, Any]], damage: str
) -> None:
    path, raw = protocol
    if damage in {"empty", "missing_final", "bool_limit", "bool_final", "bool_iteration", "bool_review"}:
        _damage_counters(raw, damage)
    elif damage == "gap":
        raw["iterations"] = raw["iterations"][1:]
    elif damage == "duplicate":
        raw["iterations"].insert(0, raw["iterations"][0])
    elif damage == "blocking":
        raw["iterations"][-1]["reviewer"]["verdict"] = "changes_requested"
        raw["iterations"][-1]["reviewer"]["issues"] = [{"severity": "error", "message": "Synthetic blocker"}]
    elif damage == "not_done":
        raw["iterations"][-1]["creator"]["done"] = False
    elif damage == "unknown_outcome":
        raw["decision"]["outcome"] = "invented"
    elif damage == "after_terminal":
        raw["iterations"].append(raw["iterations"][-1])
    text = json.dumps(raw)
    if damage == "duplicate_member":
        # The engine refuses duplicate members; status must not keep only the last (converged) one.
        text = '{"decision": null, ' + text[1:]
    path.write_text(text)
    before = path.read_bytes()
    result = CliRunner().invoke(app, ["status", "--workspace", str(tmp_path / "workspace")])
    assert result.exit_code != 0
    assert "invalid" in result.output.lower()
    assert "Outcome: converged" not in result.output
    assert path.read_bytes() == before


def _damage_counters(raw: dict[str, Any], damage: str) -> None:
    if damage == "empty":
        raw["iterations"] = []
    elif damage == "missing_final":
        raw["decision"]["final_iteration"] = 3
    elif damage == "bool_limit":
        raw["max_iterations"] = True
    elif damage == "bool_final":
        raw["iterations"] = [raw["iterations"][-1]]
        raw["iterations"][0]["iteration"] = 1
        raw["iterations"][0]["reviewer"]["iteration"] = 1
        raw["decision"]["final_iteration"] = True
    elif damage == "bool_iteration":
        raw["iterations"][0]["iteration"] = True
    elif damage == "bool_review":
        raw["iterations"][0]["reviewer"]["iteration"] = True


@pytest.mark.parametrize("outcome", ["converged", "in_progress", "needs_human"])
def test_status_accepts_engine_histories(tmp_path: Path, protocol: tuple[Path, dict[str, Any]], outcome: str) -> None:
    path, raw = protocol
    if outcome == "in_progress":
        raw["iterations"] = raw["iterations"][:1]
        raw["decision"] = None
    elif outcome == "needs_human":
        raw["decision"]["outcome"] = "needs_human"
        raw["decision"]["reason"] = "selection_conflict"
    path.write_text(json.dumps(raw))
    result = CliRunner().invoke(app, ["status", "--workspace", str(tmp_path / "workspace")])
    assert result.exit_code == 0, result.output
    assert f"Outcome: {outcome}" in result.output


@pytest.mark.parametrize("redirected", ["copy/protocol", "copy/protocol/description"])
def test_status_refuses_redirected_protocol_directory(
    tmp_path: Path, protocol: tuple[Path, dict[str, Any]], redirected: str
) -> None:
    workspace = tmp_path / "workspace"
    result = CliRunner().invoke(app, ["status", "--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    link = workspace / redirected
    target = tmp_path / "redirected-target"
    link.rename(target)
    link.symlink_to(target, target_is_directory=True)

    result = CliRunner().invoke(app, ["status", "--workspace", str(workspace)])

    assert result.exit_code != 0, result.output
    assert "Invalid protocol state" in result.output
    assert "No protocol state files found" not in result.output
