from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from podcast_pipeline.asset_refine_llm import (
    REFINE_PROVENANCE_KIND,
    RefineRequest,
    build_refine_prompt,
    build_refine_request,
    parse_refined_candidate,
    refine_asset_text,
    refine_instruction,
)
from podcast_pipeline.domain.models import Candidate, TextFormat
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


class _FakeDrafter:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def run(self, prompt_text: str) -> dict[str, Any]:
        self.prompts.append(prompt_text)
        return self.reply


def test_refine_prompt_contains_instruction_text_notes_and_format_rules() -> None:
    prompt = build_refine_prompt(
        asset_id="slug",
        source_text="# Slug\n\npython-neuigkeiten\n",
        instruction="Slug nur python-315",
        editorial_notes="Immer kurz halten.",
        episode_summary_markdown="Wir reden über Python 3.15.",
    )
    assert "Slug nur python-315" in prompt
    assert "python-neuigkeiten" in prompt
    assert "Immer kurz halten." in prompt
    assert "Wir reden über Python 3.15." in prompt
    assert "plain text only" in prompt
    assert "lowercase letters a-z" in prompt
    assert '"content"' in prompt


def test_refine_prompt_for_markdown_asset_skips_missing_context() -> None:
    prompt = build_refine_prompt(asset_id="description", source_text="Text", instruction="kürzer")
    assert "Return Markdown." in prompt
    assert "Episode summary" not in prompt
    assert "Standing editorial notes" not in prompt


def test_parse_refined_candidate_uses_natural_format_and_records_instruction() -> None:
    based_on = Candidate(asset_id="title_detail", content="# Titel\n\nAlter Titel")
    candidate = parse_refined_candidate(
        {"content": "  Neuer Titel \n"},
        asset_id="title_detail",
        instruction="kürzer",
        based_on_candidate_id=based_on.candidate_id,
    )
    assert candidate.format == TextFormat.plain
    assert candidate.content == "Neuer Titel"
    assert refine_instruction(candidate) == "kürzer"
    (ref,) = candidate.provenance
    assert ref.kind == REFINE_PROVENANCE_KIND
    assert ref.metadata["based_on_candidate_id"] == str(based_on.candidate_id)


def test_parse_refined_candidate_accepts_candidates_shape() -> None:
    candidate = parse_refined_candidate(
        {"candidates": [{"content": "# Neu\n\nText"}]},
        asset_id="description",
        instruction="ohne Emojis",
        based_on_candidate_id=None,
    )
    assert candidate.format == TextFormat.markdown
    assert candidate.content == "# Neu\n\nText"
    assert candidate.provenance[0].metadata["based_on_candidate_id"] is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"text": "x"}, "no 'content' string"),
        ({"content": "Python 3.15"}, "Invalid slug"),
        ({"content": "  "}, "must not be empty"),
    ],
)
def test_parse_refined_candidate_rejects_unusable_answers(payload: dict[str, Any], message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        parse_refined_candidate(payload, asset_id="slug", instruction="x", based_on_candidate_id=None)


def test_build_refine_request_sources() -> None:
    candidate = Candidate(asset_id="description", content="Kandidat")

    from_candidate = build_refine_request(
        asset_id="description",
        instruction=" kürzer ",
        candidates=[candidate],
        candidate_id=str(candidate.candidate_id),
        text=None,
        current_selection_text="Auswahl",
        editorial_notes="",
    )
    assert from_candidate == RefineRequest(
        asset_id="description",
        instruction="kürzer",
        source_text="Kandidat",
        based_on_candidate_id=candidate.candidate_id,
    )

    from_text = build_refine_request(
        asset_id="description",
        instruction="kürzer",
        candidates=[candidate],
        candidate_id=None,
        text="Editor",
        current_selection_text="Auswahl",
    )
    assert (from_text.source_text, from_text.based_on_candidate_id) == ("Editor", None)

    from_selection = build_refine_request(
        asset_id="description",
        instruction="kürzer",
        candidates=[candidate],
        candidate_id=None,
        text=None,
        current_selection_text="Auswahl",
    )
    assert from_selection.source_text == "Auswahl"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"instruction": "  "}, "instruction must not be empty"),
        ({"instruction": "x" * 2001}, "at most 2000"),
        ({"candidate_id": "nope"}, "Invalid candidate_id"),
        ({"candidate_id": "00000000-0000-0000-0000-000000000000"}, "not found"),
        ({"candidate_id": "00000000-0000-0000-0000-000000000000", "text": "x"}, "either candidate_id or text"),
        ({"text": "  "}, "no text to refine"),
        ({"current_selection_text": None}, "no text to refine"),
    ],
)
def test_build_refine_request_rejects_bad_input(kwargs: dict[str, Any], message: str) -> None:
    args: dict[str, Any] = {
        "asset_id": "description",
        "instruction": "kürzer",
        "candidates": [],
        "candidate_id": None,
        "text": None,
        "current_selection_text": None,
    }
    args.update(kwargs)
    with pytest.raises(ValueError, match=message):
        build_refine_request(**args)


def test_refine_asset_text_runs_drafter_with_summary_context(tmp_path: Path) -> None:
    summary_path = EpisodeWorkspaceLayout(root=tmp_path).episode_summary_json_path()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps({"summary_markdown": "Python 3.15 Release", "key_points": [], "topics": []}),
        encoding="utf-8",
    )
    drafter = _FakeDrafter({"content": "python-315"})

    candidate = refine_asset_text(
        workspace=tmp_path,
        request=RefineRequest(asset_id="slug", instruction="Slug nur python-315", source_text="python-neuigkeiten"),
        runner=drafter,
    )

    assert candidate.content == "python-315"
    assert candidate.format == TextFormat.plain
    assert "Python 3.15 Release" in drafter.prompts[0]
