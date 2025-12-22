from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import typer

from podcast_pipeline.agent_cli_config import collect_agent_cli_issues
from podcast_pipeline.domain.models import IssueSeverity, ReviewIssue, ReviewIteration
from podcast_pipeline.review_loop_engine import LoopOutcome
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


@dataclass(frozen=True)
class _ProtocolDecision:
    outcome: str
    final_iteration: int | None
    reason: str | None


@dataclass(frozen=True)
class _ProtocolIteration:
    iteration: int
    review: ReviewIteration


@dataclass(frozen=True)
class _ProtocolState:
    asset_id: str
    max_iterations: int
    iterations: tuple[_ProtocolIteration, ...]
    decision: _ProtocolDecision | None


@dataclass(frozen=True)
class _AssetStatus:
    asset_id: str
    iteration: int | None
    max_iterations: int
    verdict: str | None
    outcome: str
    decision_reason: str | None
    blocking_issues: tuple[ReviewIssue, ...]
    outstanding_issues: tuple[ReviewIssue, ...]


def run_status(*, workspace: Path) -> None:
    issues = collect_agent_cli_issues(workspace=workspace)
    for issue in issues:
        typer.echo(issue, err=True)

    layout = EpisodeWorkspaceLayout(root=workspace)
    protocol_states = _find_protocol_states(layout)
    if not protocol_states:
        typer.echo(f"No protocol state files found under {layout.copy_protocol_dir}")
        return

    statuses = [_build_status(state) for state in protocol_states]
    statuses.sort(key=lambda status: status.asset_id)

    lines = [f"Workspace: {workspace.resolve()}"]
    for status in statuses:
        lines.extend(_render_status(status))
    typer.echo("\n".join(lines))


def _find_protocol_states(layout: EpisodeWorkspaceLayout) -> list[_ProtocolState]:
    protocol_root = layout.copy_protocol_dir
    if not protocol_root.exists():
        return []
    states: list[_ProtocolState] = []
    for path in sorted(protocol_root.glob("*/state.json")):
        states.append(_load_protocol_state(path))
    return states


def _load_protocol_state(path: Path) -> _ProtocolState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid JSON at {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise typer.BadParameter(f"Expected object JSON at {path}")

    asset_id = _require_str(raw, "asset_id", path)
    max_iterations = _require_int(raw, "max_iterations", path)

    iterations_raw = raw.get("iterations", [])
    if not isinstance(iterations_raw, list):
        raise typer.BadParameter(f"Expected list for iterations at {path}")

    iterations: list[_ProtocolIteration] = []
    for item in iterations_raw:
        if not isinstance(item, dict):
            raise typer.BadParameter(f"Expected object iteration at {path}")
        iteration = _require_int(item, "iteration", path)
        review_raw = item.get("reviewer")
        if not isinstance(review_raw, dict):
            raise typer.BadParameter(f"Expected reviewer object at {path}")
        review = ReviewIteration.model_validate(review_raw)
        if review.iteration != iteration:
            raise typer.BadParameter(
                f"Iteration mismatch at {path}: iteration={iteration} review.iteration={review.iteration}",
            )
        iterations.append(_ProtocolIteration(iteration=iteration, review=review))

    iterations.sort(key=lambda it: it.iteration)
    decision = _parse_decision(raw.get("decision"), path)
    return _ProtocolState(
        asset_id=asset_id,
        max_iterations=max_iterations,
        iterations=tuple(iterations),
        decision=decision,
    )


def _parse_decision(raw: object, path: Path) -> _ProtocolDecision | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise typer.BadParameter(f"Expected decision object at {path}")
    outcome = _require_str(raw, "outcome", path)
    final_iteration = raw.get("final_iteration")
    if final_iteration is not None and not isinstance(final_iteration, int):
        raise typer.BadParameter(f"Expected final_iteration int at {path}")
    reason = raw.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise typer.BadParameter(f"Expected reason string at {path}")
    return _ProtocolDecision(
        outcome=outcome,
        final_iteration=final_iteration,
        reason=reason,
    )


def _require_str(raw: dict[str, object], key: str, path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise typer.BadParameter(f"Expected non-empty string for {key} at {path}")
    return value


def _require_int(raw: dict[str, object], key: str, path: Path) -> int:
    value = raw.get(key)
    if not isinstance(value, int):
        raise typer.BadParameter(f"Expected int for {key} at {path}")
    return value


def _build_status(state: _ProtocolState) -> _AssetStatus:
    if state.iterations:
        latest = state.iterations[-1]
        iteration = latest.iteration
        verdict = latest.review.verdict.value
        issues = tuple(latest.review.issues)
    else:
        iteration = None
        verdict = None
        issues = ()

    blocking = tuple(issue for issue in issues if issue.severity == IssueSeverity.error)
    outcome = state.decision.outcome if state.decision is not None else LoopOutcome.in_progress.value
    reason = state.decision.reason if state.decision is not None else None

    return _AssetStatus(
        asset_id=state.asset_id,
        iteration=iteration,
        max_iterations=state.max_iterations,
        verdict=verdict,
        outcome=outcome,
        decision_reason=reason,
        blocking_issues=blocking,
        outstanding_issues=issues,
    )


def _render_status(status: _AssetStatus) -> list[str]:
    lines = [f"Asset: {status.asset_id}"]
    lines.append(f"  Iteration: {_format_iteration(status.iteration, status.max_iterations)}")
    lines.append(f"  Verdict: {status.verdict or 'none'}")
    outcome_line = f"  Outcome: {status.outcome}"
    if status.decision_reason:
        outcome_line += f" (reason={status.decision_reason})"
    lines.append(outcome_line)
    lines.append(_format_blocking_line(status.blocking_issues))
    lines.extend(_format_issue_lines("Outstanding issues", status.outstanding_issues))
    return lines


def _format_iteration(iteration: int | None, max_iterations: int) -> str:
    if iteration is None:
        return f"0/{max_iterations}"
    return f"{iteration}/{max_iterations}"


def _format_blocking_line(issues: tuple[ReviewIssue, ...]) -> str:
    if not issues:
        return "  Blocking issues: none"
    return f"  Blocking issues: {len(issues)}"


def _format_issue_lines(title: str, issues: tuple[ReviewIssue, ...]) -> list[str]:
    if not issues:
        return [f"  {title}: none"]
    lines = [f"  {title}: {len(issues)}"]
    for issue in issues:
        lines.append(f"    - {_format_issue(issue)}")
    return lines


def _format_issue(issue: ReviewIssue) -> str:
    suffix: list[str] = []
    if issue.code:
        suffix.append(f"code={issue.code}")
    if issue.field:
        suffix.append(f"field={issue.field}")
    if suffix:
        return f"{issue.severity.value}: {issue.message} ({', '.join(suffix)})"
    return f"{issue.severity.value}: {issue.message}"
