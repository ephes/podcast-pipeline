from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import typer

from podcast_pipeline.agent_cli_config import collect_agent_cli_issues
from podcast_pipeline.domain.models import (
    AssetKind,
    EpisodeWorkspace,
    IssueSeverity,
    ReviewIssue,
    TextFormat,
)
from podcast_pipeline.file_inputs import read_workspace_utf8
from podcast_pipeline.pick_core import (
    custom_selection_format,
    has_manual_selection_provenance,
    normalize_custom_selection_text,
)
from podcast_pipeline.review_loop_engine import LoopOutcome, parse_loop_protocol_state
from podcast_pipeline.review_loop_engine import LoopProtocolState as _ProtocolState
from podcast_pipeline.tag_assets import TAG_ASSET_IDS, render_selected_tags_markdown
from podcast_pipeline.tag_parsing import parse_tag_list
from podcast_pipeline.terminal_text import terminal_text
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout

MAX_STATUS_FILE_BYTES = 8 * 1024 * 1024
MAX_STATUS_TOTAL_BYTES = 32 * 1024 * 1024
MAX_STATUS_ENTRIES = 10_000


class _IncompleteInspection(ValueError):
    pass


@dataclass
class _Inspection:
    layout: EpisodeWorkspaceLayout
    entries: int = 0
    bytes_read: int = 0

    def paths(self, directory: Path, pattern: str = "*") -> Iterator[Path]:
        if not directory.exists():
            return
        with os.scandir(directory) as entries:
            for entry in entries:
                self.entries += 1
                if self.entries > MAX_STATUS_ENTRIES:
                    raise _IncompleteInspection("directory entry limit exceeded")
                if fnmatchcase(entry.name, pattern):
                    yield directory / entry.name

    def read_text(self, path: Path) -> str:
        remaining = MAX_STATUS_TOTAL_BYTES - self.bytes_read
        size = path.lstat().st_size
        if size > remaining:
            raise _IncompleteInspection("cumulative byte limit exceeded")
        limit = min(MAX_STATUS_FILE_BYTES, remaining)
        try:
            text = read_workspace_utf8(self.layout.root, path.relative_to(self.layout.root), max_bytes=limit)
        except (OSError, ValueError):
            self.bytes_read += limit
            raise
        self.bytes_read += len(text.encode("utf-8"))
        return text

    def read_json(self, path: Path) -> Any:
        raw = json.loads(self.read_text(path))
        _check_json_depth(raw)
        return raw


def _check_json_depth(value: Any, depth: int = 0) -> None:
    if isinstance(value, (dict, list)):
        if depth >= 64:
            raise ValueError("JSON nesting limit exceeded")
        for child in value.values() if isinstance(value, dict) else value:
            _check_json_depth(child, depth + 1)


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
        typer.echo(terminal_text(issue), err=True)

    layout = EpisodeWorkspaceLayout(root=workspace)
    try:
        _inspect_status(layout)
    except (_IncompleteInspection, OSError):
        typer.echo("Status inspection incomplete: workspace entry/byte limit or directory read failure.", err=True)
        raise typer.Exit(1) from None


def _inspect_status(layout: EpisodeWorkspaceLayout) -> None:
    inspection = _Inspection(layout)
    workspace = layout.root
    protocol_states = _find_protocol_states(layout, inspection)

    statuses: list[_AssetStatus] = []
    if not protocol_states:
        lines = [
            f"Workspace: {workspace.resolve()}",
            f"No protocol state files found under {layout.copy_protocol_dir}",
        ]
    else:
        statuses = [_build_status(state) for state in protocol_states]
        statuses.sort(key=lambda status: status.asset_id)
        lines = [f"Workspace: {workspace.resolve()}"]
        for status in statuses:
            lines.extend(_render_status(status))

    checklist_lines, next_steps = _build_checklist(
        layout=layout, statuses=statuses, protocol_states=protocol_states, inspection=inspection
    )
    if checklist_lines:
        lines.append("Checklist:")
        lines.extend(checklist_lines)
    if next_steps:
        lines.append("Next steps:")
        lines.extend(next_steps)
    typer.echo("\n".join(terminal_text(line) for line in lines))


def _find_protocol_states(layout: EpisodeWorkspaceLayout, inspection: _Inspection) -> list[_ProtocolState]:
    protocol_root = layout.copy_protocol_dir
    if not protocol_root.exists():
        return []
    states: list[_ProtocolState] = []
    for directory in inspection.paths(protocol_root):
        if directory.is_dir():
            for path in inspection.paths(directory, "state.json"):
                state = _load_protocol_state(path, inspection)
                if state.asset_id != directory.name or state.asset_id not in _required_asset_ids():
                    raise typer.BadParameter("Invalid protocol directory/asset identity")
                if any(previous.asset_id == state.asset_id for previous in states):
                    raise typer.BadParameter("Invalid duplicate protocol asset evidence")
                states.append(state)
    return states


def _build_checklist(
    *,
    layout: EpisodeWorkspaceLayout,
    statuses: list[_AssetStatus],
    protocol_states: list[_ProtocolState],
    inspection: _Inspection,
) -> tuple[list[str], list[str]]:
    checklist: list[str] = []
    next_steps: list[str] = []

    workspace_state, state_error = _load_workspace_state(layout, inspection)
    state_status = "ok" if state_error is None and layout.state_json.exists() else "missing"
    if state_error is not None:
        state_status = "invalid"
    checklist.extend(
        [
            _format_check("episode.yaml", "ok" if layout.episode_yaml.exists() else "missing"),
            _format_check("state.json", state_status),
        ],
    )

    transcript_path = layout.transcript_dir / "transcript.txt"
    transcript_ok = transcript_path.exists()
    checklist.append(_format_check("transcript/transcript.txt", "ok" if transcript_ok else "missing"))

    chunk_text_count = _glob_count(layout.transcript_chunks_dir, "chunk_*.txt", inspection)
    chunk_meta_count = _glob_count(layout.transcript_chunks_dir, "chunk_*.json", inspection)
    chunk_summary_count = _glob_count(layout.chunk_summaries_dir, "chunk_*.summary.json", inspection)
    checklist.append(_format_count_check("transcript/chunks/*.txt", chunk_text_count))
    checklist.append(_format_count_check("transcript/chunks/*.json", chunk_meta_count))
    checklist.append(_format_count_check("summaries/chunks/*.summary.json", chunk_summary_count))

    summary_json = layout.episode_summary_json_path()
    summary_md = layout.episode_summary_markdown_path()
    summary_html = layout.episode_summary_html_path()
    summary_ok = summary_json.exists()
    checklist.extend(
        [
            _format_check("summaries/episode/episode_summary.json", "ok" if summary_ok else "missing"),
            _format_check("summaries/episode/episode_summary.md", "ok" if summary_md.exists() else "missing"),
            _format_check("summaries/episode/episode_summary.html", "ok" if summary_html.exists() else "missing"),
        ],
    )

    required_assets = _required_asset_ids()
    candidate_assets = _candidate_assets(layout, inspection)
    missing_candidates = sorted(asset for asset in required_assets if asset not in candidate_assets)
    checklist.append(_format_asset_check("copy/candidates", missing_candidates))

    selected_assets = _selected_assets(layout, inspection, workspace_state)
    missing_selections = sorted(asset for asset in required_assets if asset not in selected_assets)
    blocked_selections = sorted(asset for asset in missing_selections if asset in missing_candidates)
    pending_selections = sorted(asset for asset in missing_selections if asset not in missing_candidates)
    checklist.append(
        _format_selection_check(
            missing=missing_selections,
            pending=pending_selections,
            blocked=blocked_selections,
            state=workspace_state,
            state_error=state_error,
        ),
    )

    review_lines, review_steps = _review_checks(
        required_assets=required_assets,
        protocol_states=protocol_states,
        statuses=statuses,
    )
    checklist.extend(review_lines)
    next_steps.extend(review_steps)

    if state_error is not None:
        next_steps.append("Fix invalid state.json before updating selections.")

    if not transcript_ok:
        next_steps.append(f"Add a transcript at {transcript_path}.")
    else:
        if chunk_text_count == 0 or chunk_meta_count == 0:
            next_steps.append(f"Generate transcript chunks under {layout.transcript_chunks_dir}.")
        if summary_json.exists() is False:
            next_steps.append(f"Generate episode summaries under {layout.episode_summary_dir}.")

    if missing_candidates:
        if summary_ok:
            next_steps.append(f"Run `podcast draft-candidates --workspace {layout.root}` to create copy candidates.")
        else:
            next_steps.append("Create episode summaries before drafting copy candidates.")

    if pending_selections:
        if missing_candidates:
            next_steps.append("Select copy once candidate drafts are available.")
        else:
            next_steps.append(f"Run `podcast pick --workspace {layout.root}` to select final copy.")

    return _dedupe_lines(checklist), _dedupe_lines(next_steps)


def _load_workspace_state(
    layout: EpisodeWorkspaceLayout,
    inspection: _Inspection,
) -> tuple[EpisodeWorkspace | None, str | None]:
    if not layout.state_json.exists():
        return None, None
    try:
        return EpisodeWorkspace.model_validate(inspection.read_json(layout.state_json)), None
    except _IncompleteInspection:
        raise
    except (OSError, ValueError, RecursionError):
        return None, "Invalid state.json: expected bounded regular UTF-8 workspace JSON"


def _required_asset_ids() -> tuple[str, ...]:
    return tuple(kind.value for kind in AssetKind)


def _glob_count(path: Path, pattern: str, inspection: _Inspection) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in inspection.paths(path, pattern))


def _candidate_assets(layout: EpisodeWorkspaceLayout, inspection: _Inspection) -> set[str]:
    root = layout.copy_candidates_dir
    if not root.exists():
        return set()
    assets: set[str] = set()
    for asset_dir in inspection.paths(root):
        if not asset_dir.is_dir():
            continue
        if _glob_count(asset_dir, "candidate_*.json", inspection) > 0:
            assets.add(asset_dir.name)
    return assets


def _selected_assets(
    layout: EpisodeWorkspaceLayout, inspection: _Inspection, state: EpisodeWorkspace | None
) -> set[str]:
    # Admit every entry, including irrelevant files, before declaring readiness.
    list(inspection.paths(layout.copy_selected_dir))
    if state is None:
        return set()
    assets = {asset.asset_id: asset for asset in state.assets}
    selected: set[str] = set()
    for asset_id in _required_asset_ids():
        asset = assets.get(asset_id)
        candidate = (
            next((c for c in asset.candidates if c.candidate_id == asset.selected_candidate_id), None)
            if asset is not None
            else None
        )
        if candidate is not None:
            fmt, expected = candidate.format, candidate.content
        elif has_manual_selection_provenance(state, asset_id):
            fmt, expected = custom_selection_format(asset_id), None
        elif asset_id in TAG_ASSET_IDS:
            fmt, expected = TextFormat.markdown, None
        else:
            continue
        try:
            if not _selected_text_agrees(layout, inspection, asset_id, fmt, expected):
                continue
        except _IncompleteInspection:
            raise
        except (OSError, ValueError):
            continue
        selected.add(asset_id)
    return selected


def _selected_text_agrees(
    layout: EpisodeWorkspaceLayout, inspection: _Inspection, asset_id: str, fmt: TextFormat, expected: str | None
) -> bool:
    text = inspection.read_text(layout.selected_text_path(asset_id, fmt))
    if not text.strip() or _has_conflicting_format(layout, asset_id, fmt):
        return False
    if expected is not None:
        if text == (expected if expected.endswith("\n") else expected + "\n"):
            return True
        return (
            asset_id in TAG_ASSET_IDS
            and fmt == TextFormat.markdown
            and text == render_selected_tags_markdown(asset_id, parse_tag_list(expected))
        )
    if asset_id in TAG_ASSET_IDS:
        return True
    return text == normalize_custom_selection_text(asset_id, text) + "\n"


def _has_conflicting_format(layout: EpisodeWorkspaceLayout, asset_id: str, fmt: TextFormat) -> bool:
    allowed = {fmt, TextFormat.html} if fmt == TextFormat.markdown else {fmt}
    return any(layout.selected_text_path(asset_id, other).exists() for other in TextFormat if other not in allowed)


def _format_check(label: str, status: str) -> str:
    return f"  - {label}: {status}"


def _format_count_check(label: str, count: int) -> str:
    status = "ok" if count > 0 else "missing"
    detail = f"{count} file(s)" if count > 0 else "none found"
    return f"  - {label}: {status} ({detail})"


def _format_asset_check(label: str, missing: list[str]) -> str:
    if not missing:
        return f"  - {label}: ok"
    return f"  - {label}: missing for {', '.join(missing)}"


def _format_selection_check(
    *,
    missing: list[str],
    pending: list[str],
    blocked: list[str],
    state: EpisodeWorkspace | None,
    state_error: str | None,
) -> str:
    if state_error is not None:
        return "  - copy/selected: blocked (state.json invalid)"
    if not missing:
        return "  - copy/selected: ok"
    details: list[str] = []
    if pending:
        details.append(f"needs pick: {', '.join(pending)}")
    if blocked:
        details.append(f"blocked (no candidates): {', '.join(blocked)}")
    if state is not None:
        missing_state = _missing_selected_in_state(state=state, missing=missing)
        if missing_state:
            details.append(f"not recorded in state.json: {', '.join(missing_state)}")
    detail_text = "; ".join(details) if details else "missing selections"
    return f"  - copy/selected: missing ({detail_text})"


def _missing_selected_in_state(*, state: EpisodeWorkspace, missing: list[str]) -> list[str]:
    selection_by_asset = {
        asset.asset_id: asset.selected_candidate_id
        for asset in state.assets
        if asset.selected_candidate_id is not None
    }
    known_assets = {asset.asset_id for asset in state.assets}
    return [
        asset_id
        for asset_id in missing
        if asset_id in known_assets
        and asset_id not in selection_by_asset
        and not has_manual_selection_provenance(state, asset_id)
    ]


def _review_checks(
    *,
    required_assets: tuple[str, ...],
    protocol_states: list[_ProtocolState],
    statuses: list[_AssetStatus],
) -> tuple[list[str], list[str]]:
    state_by_asset = {state.asset_id: state for state in protocol_states}
    status_by_asset = {status.asset_id: status for status in statuses}
    buckets = _collect_review_buckets(required_assets=required_assets, state_by_asset=state_by_asset)
    lines = _review_summary_lines(buckets)
    steps = _review_next_steps(buckets, status_by_asset)
    return lines, steps


def _collect_review_buckets(
    *,
    required_assets: tuple[str, ...],
    state_by_asset: dict[str, _ProtocolState],
) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {
        "converged": [],
        "needs_human": [],
        "in_progress": [],
        "missing": [],
    }
    for asset_id in required_assets:
        bucket = _review_bucket_for_state(state_by_asset.get(asset_id))
        buckets[bucket].append(asset_id)
    return buckets


def _review_bucket_for_state(state: _ProtocolState | None) -> str:
    if state is None:
        return "missing"
    if state.decision is None:
        return "in_progress"
    outcome = state.decision.outcome
    if outcome == LoopOutcome.converged.value:
        return "converged"
    if outcome == LoopOutcome.needs_human.value:
        return "needs_human"
    return "in_progress"


def _review_summary_lines(buckets: dict[str, list[str]]) -> list[str]:
    missing = buckets["missing"]
    in_progress = buckets["in_progress"]
    needs_human = buckets["needs_human"]
    converged = buckets["converged"]

    if not (missing or in_progress or needs_human):
        return ["  - review convergence: ok"]

    status = "blocked" if needs_human else "in_progress"
    details: list[str] = []
    if needs_human:
        details.append(f"needs_human: {', '.join(sorted(needs_human))}")
    if in_progress:
        details.append(f"in_progress: {', '.join(sorted(in_progress))}")
    if missing:
        details.append(f"missing: {', '.join(sorted(missing))}")

    lines = [f"  - review convergence: {status} ({'; '.join(details)})"]
    if converged:
        lines.append(f"  - review converged: {', '.join(sorted(converged))}")
    return lines


def _review_next_steps(
    buckets: dict[str, list[str]],
    status_by_asset: dict[str, _AssetStatus],
) -> list[str]:
    missing = buckets["missing"]
    in_progress = buckets["in_progress"]
    needs_human = buckets["needs_human"]

    steps: list[str] = []
    if needs_human:
        steps.append(f"Manual review needed for: {', '.join(sorted(needs_human))}.")
    if in_progress:
        blocked_assets = _blocked_assets(status_by_asset, in_progress)
        if blocked_assets:
            steps.append(
                f"Resolve blocking review issues for: {', '.join(sorted(blocked_assets))}.",
            )
        steps.append(f"Continue review loop for: {', '.join(sorted(in_progress))}.")
    if missing:
        steps.append(f"Run review loop for: {', '.join(sorted(missing))}.")
    return steps


def _blocked_assets(
    status_by_asset: dict[str, _AssetStatus],
    assets: list[str],
) -> list[str]:
    blocked: list[str] = []
    for asset_id in assets:
        status = status_by_asset.get(asset_id)
        if status is None:
            continue
        if status.blocking_issues:
            blocked.append(asset_id)
    return blocked


def _dedupe_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def _load_protocol_state(path: Path, inspection: _Inspection) -> _ProtocolState:
    try:
        return parse_loop_protocol_state(inspection.read_json(path))
    except _IncompleteInspection:
        raise
    except (OSError, ValueError, RecursionError):
        raise typer.BadParameter("Invalid protocol state: repair bounded regular UTF-8 review evidence") from None


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
