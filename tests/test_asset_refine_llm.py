from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from podcast_pipeline.asset_candidates_llm import generate_single_asset_candidates_llm
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
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore


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


@pytest.mark.parametrize(
    "payload",
    [
        {"content": "# Neu\n\nText"},
        {"candidate": {"content": "# Neu\n\nText"}},
        {"candidate": [{"content": "# Neu\n\nText"}]},
        {"candidates": {"content": "# Neu\n\nText"}},
        {"candidates": [{"content": "# Neu\n\nText"}]},
    ],
)
def test_parse_refined_candidate_accepts_single_result(payload: dict[str, object]) -> None:
    candidate = parse_refined_candidate(
        payload,
        asset_id="description",
        instruction="ohne Emojis",
        based_on_candidate_id=None,
    )
    assert candidate.format == TextFormat.markdown
    assert candidate.content == "# Neu\n\nText"
    assert candidate.provenance[0].metadata["based_on_candidate_id"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"candidates": [{"content": "Erster"}, {"content": "Zweiter"}]},
        {"candidate": [{"content": "Erster"}, {"content": "Zweiter"}]},
        {"content": "Erster", "candidates": [{"content": "Zweiter"}]},
        {"content": "Erster", "candidate": {"content": "Zweiter"}},
        {"candidate": {"content": "Erster"}, "candidates": [{"content": "Zweiter"}]},
    ],
)
def test_parse_refined_candidate_rejects_ambiguous_envelopes(payload: dict[str, object]) -> None:
    with pytest.raises(RuntimeError, match="exactly one refinement result"):
        parse_refined_candidate(payload, asset_id="description", instruction="kurz", based_on_candidate_id=None)


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


def test_build_refine_request_sources(tmp_path: Path) -> None:
    candidate = Candidate(asset_id="description", content="Kandidat")
    other = Candidate(asset_id="shownotes", content="Andere Vorlage")

    from_candidate = build_refine_request(
        asset_id="description",
        instruction=" kürzer ",
        candidates=[other, candidate],
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

    drafter = _FakeDrafter({"content": "Neu"})
    refined = refine_asset_text(workspace=tmp_path, request=from_candidate, runner=drafter)
    assert refined.asset_id == candidate.asset_id
    (ref,) = refined.provenance
    assert ref.metadata["based_on_candidate_id"] == str(candidate.candidate_id)
    (prompt,) = drafter.prompts
    assert "Kandidat" in prompt
    assert other.content not in prompt

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


def test_build_refine_request_rejects_candidate_from_another_asset(tmp_path: Path) -> None:
    requested = Candidate(asset_id="description", content="Beschreibung")
    other = Candidate(asset_id="shownotes", content="Andere Vorlage")
    drafter = _FakeDrafter({"content": "Neu"})

    with pytest.raises(ValueError, match="not found for asset description"):
        request = build_refine_request(
            asset_id=requested.asset_id,
            instruction="kürzer",
            candidates=[requested, other],
            candidate_id=str(other.candidate_id),
            text=None,
            current_selection_text=requested.content,
        )
        refine_asset_text(workspace=tmp_path, request=request, runner=drafter)

    assert drafter.prompts == []


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


@pytest.mark.parametrize(
    ("summary_json", "draft_error", "warns"),
    [
        (b'{"summary_markdown":"Episode context"}', None, False),
        (None, "Episode summary not found", False),
        (b'{"summary_markdown":"First","summary_markdown":"Episode context"}', "summary JSON evidence", True),
    ],
    ids=["valid", "missing", "duplicate-member"],
)
def test_refine_and_drafting_agree_on_summary_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    summary_json: bytes | None,
    draft_error: str | None,
    warns: bool,
) -> None:
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(tmp_path / "absent.yaml"))
    EpisodeWorkspaceStore(tmp_path).write_episode_yaml({"episode_id": "synthetic"})
    if summary_json is not None:
        _write_optional_summary(tmp_path, summary_json)
    draft_drafter = _FakeDrafter({"candidates": [{"content": "python-neuigkeiten"}]})
    monkeypatch.setattr("podcast_pipeline.drafter_runner.DrafterCliRunner", lambda **kwargs: draft_drafter)

    if draft_error is None:
        generate_single_asset_candidates_llm(workspace=tmp_path, asset_id="slug", candidates_per_asset=1)
        (draft_prompt,) = draft_drafter.prompts
        assert "Episode context" in draft_prompt
    else:
        with pytest.raises(RuntimeError, match=draft_error):
            generate_single_asset_candidates_llm(workspace=tmp_path, asset_id="slug", candidates_per_asset=1)
        assert draft_drafter.prompts == []

    refine_drafter = _FakeDrafter({"content": "python-315"})
    candidate = refine_asset_text(
        workspace=tmp_path,
        request=RefineRequest(asset_id="slug", instruction="Slug nur python-315", source_text="python-neuigkeiten"),
        runner=refine_drafter,
    )

    assert candidate.content == "python-315"
    assert candidate.format == TextFormat.plain
    (refine_prompt,) = refine_drafter.prompts
    assert ("Episode context" in refine_prompt) is (draft_error is None)
    assert ("Episode summary (context only)" in refine_prompt) is (draft_error is None)
    assert ("omitted the optional episode summary" in capsys.readouterr().err) is warns


def _write_optional_summary(workspace: Path, content: bytes) -> Path:
    path = EpisodeWorkspaceLayout(root=workspace).episode_summary_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_refine_fifo_summary_does_not_block(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    path = _write_optional_summary(tmp_path, b"")
    path.unlink()
    os.mkfifo(path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from podcast_pipeline.asset_refine_llm import _episode_summary_markdown; "
            f"assert _episode_summary_markdown(Path({str(tmp_path)!r})) is None",
        ],
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert b"omitted the optional episode summary" in result.stderr


@pytest.mark.parametrize(
    "content",
    [
        b'{"summary_markdown":"' + b"x" * (1024 * 1024) + b'"}',
        b"[" * 2000 + b"]" * 2000,
        b"\xff",
        b'{"summary_markdown":42}',
    ],
    ids=["oversized", "deep-json", "invalid-utf8", "invalid-shape"],
)
def test_refine_omits_unusable_summary_with_safe_diagnostic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes
) -> None:
    _write_optional_summary(tmp_path, content)
    drafter = _FakeDrafter({"content": "Neu"})
    refine_asset_text(
        workspace=tmp_path,
        request=RefineRequest(asset_id="description", instruction="kurz", source_text="Alt"),
        runner=drafter,
    )
    assert "Episode summary (context only)" not in drafter.prompts[0]
    assert "omitted the optional episode summary" in capsys.readouterr().err


@pytest.mark.parametrize("stream_error", [BrokenPipeError, ValueError])
def test_refine_continues_when_diagnostic_stream_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream_error: type[Exception]
) -> None:
    _write_optional_summary(tmp_path, b'{"summary_markdown":42}')
    failed_writes: list[str] = []

    class FailingStderr(io.StringIO):
        def write(self, text: str) -> int:
            if not text:
                return super().write(text)
            failed_writes.append(text.strip())
            raise stream_error("Diagnostic stream unavailable")

    monkeypatch.setattr(sys, "stderr", FailingStderr())
    base = Candidate(asset_id="description", content="Alter Text")
    drafter = _FakeDrafter({"content": "Neu"})
    refined = refine_asset_text(
        workspace=tmp_path,
        request=RefineRequest(
            asset_id=base.asset_id,
            instruction="kurz",
            source_text=base.content,
            based_on_candidate_id=base.candidate_id,
        ),
        runner=drafter,
    )

    (prompt,) = drafter.prompts
    assert "Episode summary (context only)" not in prompt
    assert len(failed_writes) == 2
    assert "omitted the optional episode summary" in failed_writes[0]
    assert failed_writes[1] == "Refining description: kurz"
    assert refined.asset_id == base.asset_id
    assert refined.content == "Neu"
    (ref,) = refined.provenance
    assert ref.kind == REFINE_PROVENANCE_KIND
    assert ref.ref == f"refine_v1:{base.candidate_id}"
    assert ref.metadata == {"instruction": "kurz", "based_on_candidate_id": str(base.candidate_id)}


def test_refine_omits_summary_changed_during_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from podcast_pipeline.asset_refine_llm import _episode_summary_markdown

    before = json.dumps({"summary_markdown": "A" * 100_000}).encode()
    after = before.replace(b"A", b"B")
    path = _write_optional_summary(tmp_path, before)
    real_read = os.read
    changed = False

    def read_and_change(fd: int, count: int) -> bytes:
        nonlocal changed
        chunk = real_read(fd, count)
        if not changed:
            changed = True
            path.write_bytes(after)
        return chunk

    monkeypatch.setattr(os, "read", read_and_change)
    assert _episode_summary_markdown(tmp_path) is None
    assert path.read_bytes() == after


@pytest.mark.parametrize("oversized_part", ["source", "notes", "summary", "combined"])
def test_refine_rejects_complete_oversized_prompt_without_writes(tmp_path: Path, oversized_part: str) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    candidate = Candidate(asset_id="description", content="Vorher")
    candidate_path = store.write_candidate(candidate)
    selection = store.write_selected_text("description", TextFormat.markdown, candidate.content)
    preserved = {path: path.read_bytes() for path in (candidate_path, selection)}
    large = "ü" * (128 * 1024)
    parts = {"source": "Alt", "notes": "Notiz", "summary": "Zusammenfassung"}
    if oversized_part == "combined":
        parts = dict.fromkeys(parts, "ü" * (44 * 1024))
    else:
        parts[oversized_part] = large
    _write_optional_summary(tmp_path, json.dumps({"summary_markdown": parts["summary"]}).encode())
    drafter = _FakeDrafter({"content": "Neu"})
    with pytest.raises(ValueError, match="Refinement prompt exceeds 262144 UTF-8 bytes"):
        refine_asset_text(
            workspace=tmp_path,
            request=RefineRequest(
                asset_id="description", instruction="kurz", source_text=parts["source"], editorial_notes=parts["notes"]
            ),
            runner=drafter,
        )
    assert drafter.prompts == []
    assert {path: path.read_bytes() for path in preserved} == preserved


def test_refine_prompt_utf8_exact_boundary(tmp_path: Path) -> None:
    limit = 256 * 1024
    base = build_refine_prompt(asset_id="description", source_text="ü", instruction="kurz")
    source = "ü" + "x" * (limit - len(base.encode("utf-8")))
    drafter = _FakeDrafter({"content": "Neu"})
    refine_asset_text(
        workspace=tmp_path,
        request=RefineRequest(asset_id="description", instruction="kurz", source_text=source),
        runner=drafter,
    )
    assert len(drafter.prompts[0].encode("utf-8")) == limit
    with pytest.raises(ValueError, match="Refinement prompt exceeds"):
        refine_asset_text(
            workspace=tmp_path,
            request=RefineRequest(asset_id="description", instruction="kurz", source_text=source + "x"),
            runner=drafter,
        )
    assert len(drafter.prompts) == 1


@pytest.mark.parametrize("extra_byte", [False, True])
def test_optional_summary_size_boundary_admitted_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_byte: bool
) -> None:
    import os

    from podcast_pipeline.asset_refine_llm import _episode_summary_markdown

    raw = b'{"summary_markdown":"Stable context"}'
    path = _write_optional_summary(tmp_path, raw + b" " * (1024 * 1024 - len(raw) + extra_byte))
    real_read = os.read
    read_calls = 0

    def observe_read(fd: int, count: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return real_read(fd, count)

    monkeypatch.setattr(os, "read", observe_read)
    assert _episode_summary_markdown(tmp_path) == (None if extra_byte else "Stable context")
    assert (read_calls == 0) is extra_byte
    assert path.stat().st_size == 1024 * 1024 + extra_byte
