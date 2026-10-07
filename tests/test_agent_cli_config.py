from __future__ import annotations

import sys
from pathlib import Path

import pytest

from podcast_pipeline.agent_cli_config import (
    AGENT_TIMEOUT_ENV,
    DEFAULT_AGENT_TIMEOUT_SECONDS,
    AgentCliBundle,
    AgentCliConfig,
    AgentCliConfigError,
    _find_missing_cli_issues,
    load_agent_cli_bundle,
    resolve_agent_timeout,
)

# Use the current Python interpreter as a known-good executable on all platforms.
_EXISTING_CMD = sys.executable


def _bundle_with_commands(
    *,
    creator: str = _EXISTING_CMD,
    reviewer: str = _EXISTING_CMD,
    drafter: str = _EXISTING_CMD,
) -> AgentCliBundle:
    return AgentCliBundle(
        creator=AgentCliConfig(role="creator", command=creator),
        reviewer=AgentCliConfig(role="reviewer", command=reviewer),
        drafter=AgentCliConfig(role="drafter", command=drafter),
    )


def test_find_missing_cli_issues_all_roles() -> None:
    bundle = _bundle_with_commands(drafter="nonexistent_drafter_binary_xyz")
    issues = _find_missing_cli_issues(bundle)
    assert len(issues) == 1
    assert issues[0].role == "drafter"


def test_find_missing_cli_issues_scoped_to_creator_reviewer() -> None:
    bundle = _bundle_with_commands(drafter="nonexistent_drafter_binary_xyz")
    issues = _find_missing_cli_issues(bundle, roles=("creator", "reviewer"))
    assert len(issues) == 0


def test_find_missing_cli_issues_scoped_to_drafter() -> None:
    bundle = _bundle_with_commands(drafter="nonexistent_drafter_binary_xyz")
    issues = _find_missing_cli_issues(bundle, roles=("drafter",))
    assert len(issues) == 1
    assert issues[0].role == "drafter"


def test_find_missing_cli_issues_no_roles_checks_all() -> None:
    bundle = _bundle_with_commands(
        creator="nonexistent_creator_xyz",
        drafter="nonexistent_drafter_xyz",
    )
    issues = _find_missing_cli_issues(bundle)
    roles = {issue.role for issue in issues}
    assert roles == {"creator", "drafter"}


# --- agent CLI timeout resolution ---------------------------------------------


def test_resolve_agent_timeout_defaults_to_fifteen_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AGENT_TIMEOUT_ENV, raising=False)
    assert DEFAULT_AGENT_TIMEOUT_SECONDS == 900
    assert resolve_agent_timeout(None) == 900.0
    assert resolve_agent_timeout(0) == 900.0


def test_resolve_agent_timeout_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AgentCliConfig(role="drafter", command="claude", timeout_seconds=120.0)
    monkeypatch.delenv(AGENT_TIMEOUT_ENV, raising=False)
    assert resolve_agent_timeout(None, config) == 120.0
    monkeypatch.setenv(AGENT_TIMEOUT_ENV, "60")
    assert resolve_agent_timeout(None, config) == 60.0
    assert resolve_agent_timeout(5, config) == 5.0


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "nan", "inf"])
def test_resolve_agent_timeout_rejects_invalid_env(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(AGENT_TIMEOUT_ENV, raw)
    with pytest.raises(AgentCliConfigError, match=AGENT_TIMEOUT_ENV):
        resolve_agent_timeout(None)


def test_load_bundle_reads_role_timeout_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_path = tmp_path / "config.yaml"
    global_path.write_text("agents:\n  drafter:\n    timeout_seconds: 1800\n", encoding="utf-8")
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(global_path))
    bundle = load_agent_cli_bundle(workspace=None)
    assert bundle.drafter.timeout_seconds == 1800.0
    assert bundle.drafter.command == "claude"
    assert bundle.creator.timeout_seconds is None


@pytest.mark.parametrize("value", ["0", "-1", "'soon'", "true"])
def test_load_bundle_rejects_invalid_timeout_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    global_path = tmp_path / "config.yaml"
    global_path.write_text(f"agents:\n  creator:\n    timeout_seconds: {value}\n", encoding="utf-8")
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(global_path))
    with pytest.raises(AgentCliConfigError, match="agents.creator.timeout_seconds"):
        load_agent_cli_bundle(workspace=None)
