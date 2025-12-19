from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from podcast_pipeline.domain.models import Candidate, ReviewIteration
from podcast_pipeline.review_loop_engine import CreatorInput, CreatorOutput, ReviewerInput
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout

_FAKE_UUID_NAMESPACE = UUID("00000000-0000-0000-0000-000000000000")
_DEFAULT_CREATED_AT = "2000-01-01T00:00:00+00:00"


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


def _deterministic_uuid(*, prefix: str, parts: Sequence[str]) -> UUID:
    name = ":".join([prefix, *parts])
    return uuid5(_FAKE_UUID_NAMESPACE, name)


class FakeCreatorRunner:
    def __init__(
        self,
        *,
        layout: EpisodeWorkspaceLayout,
        replies: Sequence[Mapping[str, Any] | str],
        default_created_at: str = _DEFAULT_CREATED_AT,
    ) -> None:
        self._layout = layout
        self._replies = [ScriptedJsonReply.from_value(v) for v in replies]
        self._default_created_at = default_created_at
        self.calls: list[CreatorInput] = []
        self._pos = 0

    def run_json(self, inp: CreatorInput) -> dict[str, Any]:
        return self._consume(inp)

    def __call__(self, inp: CreatorInput) -> CreatorOutput:
        data = self._consume(inp)
        if "done" not in data:
            raise ValueError("FakeCreatorRunner reply must include done")
        done = bool(data["done"])

        if "candidate" in data:
            candidate_raw = data["candidate"]
            if not isinstance(candidate_raw, dict):
                raise TypeError("FakeCreatorRunner reply candidate must be an object")
            candidate_data: dict[str, Any] = dict(candidate_raw)
        else:
            candidate_data = {k: v for k, v in data.items() if k != "done"}

        candidate_data.setdefault("asset_id", inp.asset_id)
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
        reply = self._next_reply()
        _write_mutations(root=self._layout.root, mutate_files=reply.mutate_files)
        return dict(reply.json_data)

    def _next_reply(self) -> ScriptedJsonReply:
        if self._pos >= len(self._replies):
            raise IndexError(f"FakeCreatorRunner exhausted: called {self._pos + 1} times, only {len(self._replies)}")
        reply = self._replies[self._pos]
        self._pos += 1
        return reply


class FakeReviewerRunner:
    def __init__(
        self,
        *,
        layout: EpisodeWorkspaceLayout,
        replies: Sequence[Mapping[str, Any] | str],
        reviewer: str = "fake_reviewer",
        default_created_at: str = _DEFAULT_CREATED_AT,
    ) -> None:
        self._layout = layout
        self._replies = [ScriptedJsonReply.from_value(v) for v in replies]
        self._reviewer = reviewer
        self._default_created_at = default_created_at
        self.calls: list[ReviewerInput] = []
        self._pos = 0

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
        reply = self._next_reply()
        _write_mutations(root=self._layout.root, mutate_files=reply.mutate_files)
        return dict(reply.json_data)

    def _next_reply(self) -> ScriptedJsonReply:
        if self._pos >= len(self._replies):
            raise IndexError(f"FakeReviewerRunner exhausted: called {self._pos + 1} times, only {len(self._replies)}")
        reply = self._replies[self._pos]
        self._pos += 1
        return reply
