from __future__ import annotations

import sys

from podcast_pipeline.agent_cli_config import (
    AgentCliBundle,
    AgentCliConfig,
    _find_missing_cli_issues,
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
