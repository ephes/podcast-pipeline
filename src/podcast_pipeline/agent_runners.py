from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from podcast_pipeline.agent_cli_config import AgentCliBundle, AgentCliConfig
from podcast_pipeline.domain.models import Candidate, ProvenanceRef, ReviewIteration
from podcast_pipeline.prompting import (
    FewShotExample,
    GlossaryEntry,
    PromptRenderer,
    PromptRenderResult,
    PromptStore,
    default_prompt_registry,
    render_creator_prompt,
    render_reviewer_prompt,
)
from podcast_pipeline.review_loop_engine import CreatorInput, CreatorOutput, ReviewerInput
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore

_FAKE_UUID_NAMESPACE = UUID("00000000-0000-0000-0000-000000000000")
_DEFAULT_CREATED_AT = "2000-01-01T00:00:00+00:00"

GlossaryInput = Mapping[str, str] | Sequence[GlossaryEntry | Mapping[str, Any] | Sequence[str]] | None
FewShotInput = Sequence[FewShotExample | Mapping[str, Any]] | None
ScriptedReplyValue = Mapping[str, Any] | str
ScriptedReplyInput = Sequence[ScriptedReplyValue] | Mapping[str, Sequence[ScriptedReplyValue]]


class AgentRunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScriptedJsonReply:
    json_data: dict[str, Any]
    mutate_files: dict[str, str]

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | str) -> ScriptedJsonReply:
        if isinstance(value, str):
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise TypeError("Scripted JSON reply must decode to an object")
            data: dict[str, Any] = parsed
        else:
            data = dict(value)

        mutate_raw = data.pop("mutate_files", {})
        if mutate_raw is None:
            mutate_files: dict[str, str] = {}
        elif isinstance(mutate_raw, dict) and all(
            isinstance(key, str) and isinstance(file_value, str) for key, file_value in mutate_raw.items()
        ):
            mutate_files = dict(mutate_raw)
        else:
            raise TypeError("mutate_files must be a mapping of str->str")

        return cls(json_data=data, mutate_files=mutate_files)


def _write_mutations(*, root: Path, mutate_files: Mapping[str, str]) -> None:
    if not mutate_files:
        return

    root_resolved = root.resolve()
    for rel, content in sorted(mutate_files.items()):
        rel_path = Path(rel)
        if rel_path.is_absolute():
            raise ValueError(f"mutate_files path must be relative, got: {rel}")

        full = (root_resolved / rel_path).resolve()
        if not full.is_relative_to(root_resolved):
            raise ValueError(f"mutate_files path escapes root: {rel}")

        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(
            content if content.endswith("\n") else content + "\n",
            encoding="utf-8",
        )


def _parse_scripted_replies(
    replies: ScriptedReplyInput,
    *,
    label: str,
) -> tuple[list[ScriptedJsonReply] | None, dict[str, list[ScriptedJsonReply]] | None]:
    if isinstance(replies, Mapping):
        reply_map: dict[str, list[ScriptedJsonReply]] = {}
        for asset_id, asset_replies in replies.items():
            if isinstance(asset_replies, str) or not isinstance(asset_replies, Sequence):
                raise TypeError(f"{label} replies for asset '{asset_id}' must be a sequence")
            reply_map[asset_id] = [ScriptedJsonReply.from_value(v) for v in asset_replies]
        return None, reply_map

    if isinstance(replies, str) or not isinstance(replies, Sequence):
        raise TypeError(f"{label} replies must be a sequence of reply objects")
    return [ScriptedJsonReply.from_value(v) for v in replies], None


def _deterministic_uuid(*, prefix: str, parts: Sequence[str]) -> UUID:
    name = ":".join([prefix, *parts])
    return uuid5(_FAKE_UUID_NAMESPACE, name)


def _append_provenance(items: Sequence[ProvenanceRef], extra: ProvenanceRef) -> list[ProvenanceRef]:
    for item in items:
        if item.kind == extra.kind and item.ref == extra.ref:
            return list(items)
    return [*items, extra]


def _extract_json_payload(raw: str, *, label: str) -> dict[str, Any]:
    stripped = raw.strip()
    if not stripped:
        raise AgentRunnerError(f"{label} CLI output was empty")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise AgentRunnerError(f"{label} CLI output did not contain a JSON object") from exc
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AgentRunnerError(f"{label} CLI returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AgentRunnerError(f"{label} CLI JSON must be an object")
    return parsed


def _load_prompt_text(*, prompt_path: Path | None, prompt_text: str | None) -> str:
    if prompt_text is not None:
        return prompt_text
    if prompt_path is None:
        raise ValueError("prompt_path or prompt_text is required")
    return prompt_path.read_text(encoding="utf-8")


def _extract_review_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if "review" in payload:
        review_raw = payload["review"]
        if not isinstance(review_raw, Mapping):
            raise AgentRunnerError("Reviewer JSON 'review' field must be an object")
        return dict(review_raw)
    return dict(payload)


def _extract_creator_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if "creator" in payload:
        creator_raw = payload["creator"]
        if not isinstance(creator_raw, Mapping):
            raise AgentRunnerError("Creator JSON 'creator' field must be an object")
        return dict(creator_raw)
    return dict(payload)


def _require_bool(payload: Mapping[str, Any], *, key: str, label: str) -> bool:
    if key not in payload:
        raise AgentRunnerError(f"{label} JSON '{key}' field is required")
    value = payload[key]
    if not isinstance(value, bool):
        raise AgentRunnerError(f"{label} JSON '{key}' field must be a boolean")
    return value


def _parse_creator_candidate(payload: Mapping[str, Any], *, asset_id: str) -> Candidate:
    if "candidate" in payload:
        candidate_raw = payload["candidate"]
        if not isinstance(candidate_raw, Mapping):
            raise AgentRunnerError("Creator JSON 'candidate' field must be an object")
        candidate_data: dict[str, Any] = dict(candidate_raw)
    else:
        candidate_data = {k: v for k, v in payload.items() if k not in {"applied", "done"}}

    candidate_data.setdefault("asset_id", asset_id)
    if "content" not in candidate_data:
        raise AgentRunnerError("Creator JSON candidate must include content")
    if candidate_data.get("asset_id") != asset_id:
        raise AgentRunnerError("Creator candidate asset_id must match requested asset_id")
    return Candidate.model_validate(candidate_data)


def _write_creator_iteration(
    *,
    layout: EpisodeWorkspaceLayout,
    asset_id: str,
    iteration: int,
    applied: bool,
    done: bool,
    candidate: Candidate,
) -> Path:
    path = layout.creator_iteration_json_path(asset_id, iteration)
    payload = {
        "version": 1,
        "iteration": iteration,
        "asset_id": asset_id,
        "applied": applied,
        "done": done,
        "candidate_id": str(candidate.candidate_id),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class FakeCreatorRunner:
    def __init__(
        self,
        *,
        layout: EpisodeWorkspaceLayout,
        replies: ScriptedReplyInput,
        default_created_at: str = _DEFAULT_CREATED_AT,
    ) -> None:
        self._layout = layout
        self._replies, self._replies_by_asset = _parse_scripted_replies(replies, label="FakeCreatorRunner")
        self._default_created_at = default_created_at
        self.calls: list[CreatorInput] = []
        self._pos = 0
        self._pos_by_asset = {asset_id: 0 for asset_id in self._replies_by_asset or {}}

    def run_json(self, inp: CreatorInput) -> dict[str, Any]:
        return self._consume(inp)

    def __call__(self, inp: CreatorInput) -> CreatorOutput:
        data = self._consume(inp)
        creator_data = _extract_creator_payload(data)
        if "done" not in creator_data:
            raise ValueError("FakeCreatorRunner reply must include done")
        done = bool(creator_data["done"])

        if "candidate" in creator_data:
            candidate_raw = creator_data["candidate"]
            if not isinstance(candidate_raw, dict):
                raise TypeError("FakeCreatorRunner reply candidate must be an object")
            candidate_data: dict[str, Any] = dict(candidate_raw)
        else:
            candidate_data = {k: v for k, v in creator_data.items() if k not in {"done", "applied"}}

        candidate_data.setdefault("asset_id", inp.asset_id)
        if candidate_data.get("asset_id") != inp.asset_id:
            raise ValueError("FakeCreatorRunner candidate asset_id must match requested asset_id")
        if "content" not in candidate_data:
            raise ValueError("FakeCreatorRunner candidate must include content")
        candidate_data.setdefault(
            "candidate_id",
            str(
                _deterministic_uuid(
                    prefix="candidate",
                    parts=[inp.asset_id, str(inp.iteration)],
                ),
            ),
        )
        candidate_data.setdefault("created_at", self._default_created_at)

        candidate = Candidate.model_validate(candidate_data)
        return CreatorOutput(candidate=candidate, done=done)

    def _consume(self, inp: CreatorInput) -> dict[str, Any]:
        self.calls.append(inp)
        reply = self._next_reply(inp.asset_id)
        _write_mutations(root=self._layout.root, mutate_files=reply.mutate_files)
        return dict(reply.json_data)

    def _next_reply(self, asset_id: str) -> ScriptedJsonReply:
        if self._replies_by_asset is None:
            if self._replies is None or self._pos >= len(self._replies):
                total = 0 if self._replies is None else len(self._replies)
                raise IndexError(f"FakeCreatorRunner exhausted: called {self._pos + 1} times, only {total}")
            reply = self._replies[self._pos]
            self._pos += 1
            return reply

        if asset_id not in self._replies_by_asset:
            raise KeyError(f"FakeCreatorRunner has no scripted replies for asset_id '{asset_id}'")
        replies = self._replies_by_asset[asset_id]
        pos = self._pos_by_asset.get(asset_id, 0)
        if pos >= len(replies):
            raise IndexError(
                f"FakeCreatorRunner exhausted for asset '{asset_id}': called {pos + 1} times, only {len(replies)}",
            )
        reply = replies[pos]
        self._pos_by_asset[asset_id] = pos + 1
        return reply


class FakeReviewerRunner:
    def __init__(
        self,
        *,
        layout: EpisodeWorkspaceLayout,
        replies: ScriptedReplyInput,
        reviewer: str = "fake_reviewer",
        default_created_at: str = _DEFAULT_CREATED_AT,
    ) -> None:
        self._layout = layout
        self._replies, self._replies_by_asset = _parse_scripted_replies(replies, label="FakeReviewerRunner")
        self._reviewer = reviewer
        self._default_created_at = default_created_at
        self.calls: list[ReviewerInput] = []
        self._pos = 0
        self._pos_by_asset = {asset_id: 0 for asset_id in self._replies_by_asset or {}}

    def run_json(self, inp: ReviewerInput) -> dict[str, Any]:
        return self._consume(inp)

    def __call__(self, inp: ReviewerInput) -> ReviewIteration:
        data = self._consume(inp)
        if "review" in data:
            review_raw = data["review"]
            if not isinstance(review_raw, dict):
                raise TypeError("FakeReviewerRunner reply review must be an object")
            review_data: dict[str, Any] = dict(review_raw)
        else:
            review_data = data

        if "verdict" not in review_data:
            raise ValueError("FakeReviewerRunner reply must include verdict")

        review_data.setdefault("iteration", inp.iteration)
        review_data.setdefault("reviewer", self._reviewer)
        review_data.setdefault("created_at", self._default_created_at)

        issues = review_data.get("issues")
        if isinstance(issues, list):
            fixed_issues: list[dict[str, Any]] = []
            for idx, issue in enumerate(issues):
                if not isinstance(issue, dict):
                    raise TypeError("FakeReviewerRunner issues must be objects")
                issue_data = dict(issue)
                issue_data.setdefault(
                    "issue_id",
                    str(
                        _deterministic_uuid(
                            prefix="review_issue",
                            parts=[inp.asset_id, str(inp.iteration), str(idx)],
                        ),
                    ),
                )
                fixed_issues.append(issue_data)
            review_data["issues"] = fixed_issues

        review = ReviewIteration.model_validate(review_data)
        if review.iteration != inp.iteration:
            review = review.model_copy(update={"iteration": inp.iteration})
        if review.reviewer is None:
            review = review.model_copy(update={"reviewer": self._reviewer})
        return review

    def _consume(self, inp: ReviewerInput) -> dict[str, Any]:
        self.calls.append(inp)
        reply = self._next_reply(inp.asset_id)
        _write_mutations(root=self._layout.root, mutate_files=reply.mutate_files)
        return dict(reply.json_data)

    def _next_reply(self, asset_id: str) -> ScriptedJsonReply:
        if self._replies_by_asset is None:
            if self._replies is None or self._pos >= len(self._replies):
                total = 0 if self._replies is None else len(self._replies)
                raise IndexError(f"FakeReviewerRunner exhausted: called {self._pos + 1} times, only {total}")
            reply = self._replies[self._pos]
            self._pos += 1
            return reply

        if asset_id not in self._replies_by_asset:
            raise KeyError(f"FakeReviewerRunner has no scripted replies for asset_id '{asset_id}'")
        replies = self._replies_by_asset[asset_id]
        pos = self._pos_by_asset.get(asset_id, 0)
        if pos >= len(replies):
            raise IndexError(
                f"FakeReviewerRunner exhausted for asset '{asset_id}': called {pos + 1} times, only {len(replies)}",
            )
        reply = replies[pos]
        self._pos_by_asset[asset_id] = pos + 1
        return reply


class CodexCliCreatorRunner:
    def __init__(
        self,
        *,
        layout: EpisodeWorkspaceLayout,
        config: AgentCliConfig,
        timeout_seconds: float | None = None,
    ) -> None:
        self._layout = layout
        self._config = config
        self._timeout_seconds = timeout_seconds

    def run_prompt(
        self,
        *,
        prompt_path: Path | None = None,
        prompt_text: str | None = None,
        prompt_provenance: ProvenanceRef | None = None,
        asset_id: str,
        iteration: int,
    ) -> CreatorOutput:
        prompt_text = _load_prompt_text(prompt_path=prompt_path, prompt_text=prompt_text)
        raw = self._run_cli(prompt_text)
        payload = _extract_json_payload(raw, label="Creator")
        creator_data = _extract_creator_payload(payload)
        applied = _require_bool(creator_data, key="applied", label="Creator")
        done = _require_bool(creator_data, key="done", label="Creator")
        candidate = _parse_creator_candidate(creator_data, asset_id=asset_id)
        if prompt_provenance is not None:
            candidate = candidate.model_copy(
                update={"provenance": _append_provenance(candidate.provenance, prompt_provenance)},
            )
        store = EpisodeWorkspaceStore(self._layout.root)
        store.write_candidate(candidate)
        _write_creator_iteration(
            layout=self._layout,
            asset_id=asset_id,
            iteration=iteration,
            applied=applied,
            done=done,
            candidate=candidate,
        )
        return CreatorOutput(candidate=candidate, done=done)

    def run_with_prompt(
        self,
        *,
        prompt: PromptRenderResult,
        asset_id: str,
        iteration: int,
    ) -> CreatorOutput:
        store = EpisodeWorkspaceStore(self._layout.root)
        prompt_provenance = PromptStore(store).write(prompt)
        return self.run_prompt(
            prompt_text=prompt.text,
            prompt_provenance=prompt_provenance,
            asset_id=asset_id,
            iteration=iteration,
        )

    def _run_cli(self, prompt_text: str) -> str:
        command = [self._config.command, *self._config.args]
        result = subprocess.run(
            command,
            input=prompt_text,
            text=True,
            capture_output=True,
            check=False,
            cwd=str(self._layout.root),
            timeout=self._timeout_seconds,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip()
            if detail:
                detail = f" {detail}"
            raise AgentRunnerError(f"Creator CLI failed with exit code {result.returncode}.{detail}")
        if not (result.stdout or "").strip():
            raise AgentRunnerError("Creator CLI returned empty output")
        return result.stdout or ""


class ClaudeCodeReviewerRunner:
    def __init__(
        self,
        *,
        layout: EpisodeWorkspaceLayout,
        config: AgentCliConfig,
        reviewer: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._layout = layout
        self._config = config
        self._reviewer = reviewer or config.role
        self._timeout_seconds = timeout_seconds

    def run_prompt(
        self,
        *,
        prompt_path: Path | None = None,
        prompt_text: str | None = None,
        prompt_provenance: ProvenanceRef | None = None,
        asset_id: str,
        iteration: int,
    ) -> ReviewIteration:
        prompt_text = _load_prompt_text(prompt_path=prompt_path, prompt_text=prompt_text)
        raw = self._run_cli(prompt_text)
        payload = _extract_json_payload(raw, label="Reviewer")
        review_data = _extract_review_payload(payload)
        review_data.setdefault("iteration", iteration)
        review_data.setdefault("reviewer", self._reviewer)
        review = ReviewIteration.model_validate(review_data)
        if review.iteration != iteration:
            review = review.model_copy(update={"iteration": iteration})
        if review.reviewer is None:
            review = review.model_copy(update={"reviewer": self._reviewer})
        if prompt_provenance is not None:
            review = review.model_copy(
                update={"provenance": _append_provenance(review.provenance, prompt_provenance)},
            )
        store = EpisodeWorkspaceStore(self._layout.root)
        store.write_review(asset_id, review)
        return review

    def run_with_prompt(
        self,
        *,
        prompt: PromptRenderResult,
        asset_id: str,
        iteration: int,
    ) -> ReviewIteration:
        store = EpisodeWorkspaceStore(self._layout.root)
        prompt_provenance = PromptStore(store).write(prompt)
        return self.run_prompt(
            prompt_text=prompt.text,
            prompt_provenance=prompt_provenance,
            asset_id=asset_id,
            iteration=iteration,
        )

    def _run_cli(self, prompt_text: str) -> str:
        command = [self._config.command, *self._config.args]
        result = subprocess.run(
            command,
            input=prompt_text,
            text=True,
            capture_output=True,
            check=False,
            cwd=str(self._layout.root),
            timeout=self._timeout_seconds,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip()
            if detail:
                detail = f" {detail}"
            raise AgentRunnerError(f"Reviewer CLI failed with exit code {result.returncode}.{detail}")
        if not (result.stdout or "").strip():
            raise AgentRunnerError("Reviewer CLI returned empty output")
        return result.stdout or ""


def build_local_cli_runners(
    *,
    layout: EpisodeWorkspaceLayout,
    bundle: AgentCliBundle,
    renderer: PromptRenderer | None = None,
    glossary: GlossaryInput = None,
    few_shots: FewShotInput = None,
    timeout_seconds: float | None = None,
) -> tuple[Callable[[CreatorInput], CreatorOutput], Callable[[ReviewerInput], ReviewIteration]]:
    if renderer is None:
        renderer = PromptRenderer(default_prompt_registry())

    creator_runner = CodexCliCreatorRunner(
        layout=layout,
        config=bundle.creator,
        timeout_seconds=timeout_seconds,
    )
    reviewer_runner = ClaudeCodeReviewerRunner(
        layout=layout,
        config=bundle.reviewer,
        reviewer=bundle.reviewer.role,
        timeout_seconds=timeout_seconds,
    )

    def creator(inp: CreatorInput) -> CreatorOutput:
        prompt = render_creator_prompt(
            renderer=renderer,
            inp=inp,
            glossary=glossary,
            few_shots=few_shots,
        )
        return creator_runner.run_with_prompt(
            prompt=prompt,
            asset_id=inp.asset_id,
            iteration=inp.iteration,
        )

    def reviewer(inp: ReviewerInput) -> ReviewIteration:
        prompt = render_reviewer_prompt(
            renderer=renderer,
            inp=inp,
            glossary=glossary,
            few_shots=few_shots,
        )
        return reviewer_runner.run_with_prompt(
            prompt=prompt,
            asset_id=inp.asset_id,
            iteration=inp.iteration,
        )

    return creator, reviewer
