import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from podcast_pipeline import few_shot_selector as selector
from podcast_pipeline.asset_candidates_llm import generate_single_asset_candidates_llm
from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.tag_assets import TagConfigError, _load_examples
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def test_fifo_corpus_refuses_without_waiting(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    os.mkfifo(corpus)
    script = (
        "from pathlib import Path; from podcast_pipeline.tag_assets import _load_examples, TagConfigError; "
        '\ntry: _load_examples(Path(__import__("sys").argv[1]))'
        "\nexcept TagConfigError: raise SystemExit(0)"
        "\nraise SystemExit(1)"
    )
    result = subprocess.run([sys.executable, "-c", script, str(corpus)], timeout=3, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("kind", ["bytes", "line", "records"])
def test_corpus_limits_before_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    line = json.dumps({"input": "Grüße", "output": "Text"}, ensure_ascii=False) + "\n"
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(line * 3, encoding="utf-8")
    if kind == "bytes":
        monkeypatch.setattr(selector, "MAX_CORPUS_BYTES", len(line.encode()) * 2, raising=False)
    elif kind == "line":
        monkeypatch.setattr(selector, "MAX_CORPUS_LINE_BYTES", len(line.encode()) - 1, raising=False)
    else:
        monkeypatch.setattr(selector, "MAX_CORPUS_RECORDS", 2, raising=False)
    decode = Mock(side_effect=AssertionError("JSON construction before file admission"))
    monkeypatch.setattr(selector.json, "loads", decode)
    with pytest.raises(TagConfigError, match="limit"):
        _load_examples(corpus)
    decode.assert_not_called()


def test_corpus_changes_during_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('{"input":"before","output":"text"}\n')
    original = os.fstat
    calls = 0

    def fstat(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 2:
            with corpus.open("ab") as output:
                output.write(b" ")
        return original(fd)

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(TagConfigError, match="changed"):
        _load_examples(corpus)


def test_unicode_corpus_exact_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    line = json.dumps({"input": "Grüße", "output": "Text"}, ensure_ascii=False) + "\n"
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(line * 2, encoding="utf-8")
    monkeypatch.setattr(selector, "MAX_CORPUS_BYTES", len(line.encode()) * 2, raising=False)
    monkeypatch.setattr(selector, "MAX_CORPUS_LINE_BYTES", len(line.encode()), raising=False)
    monkeypatch.setattr(selector, "MAX_CORPUS_RECORDS", 2, raising=False)
    assert [record.input_text for record in _load_examples(corpus)] == ["Grüße", "Grüße"]


def test_growing_corpus_cannot_bypass_actual_byte_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('{"input":"a","output":"b"}\n')
    original = os.fstat
    grew = False

    def fstat(fd: int) -> os.stat_result:
        nonlocal grew
        before = original(fd)
        if not grew:
            grew = True
            with corpus.open("ab") as output:
                output.write(b" " * 200)
        return before

    monkeypatch.setattr(selector, "MAX_CORPUS_BYTES", 100)
    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(TagConfigError, match="limit"):
        _load_examples(corpus)


def test_corpus_selects_text_from_complete_pairs(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    records = [
        {"input": "Modern", "output": "Answer A"},
        {"user": "Legacy", "assistant": "Answer B"},
        {"input": "Both", "output": "", "user": "Both", "assistant": ""},
    ]
    corpus.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    selected = selector.select_few_shot_examples(examples=_load_examples(corpus), topics=[])

    assert [(shot.input_text, shot.output_text) for shot in selected] == [
        ("Modern", "Answer A"),
        ("Legacy", "Answer B"),
        ("Both", ""),
    ]


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"input": "PRIVATE_SENTINEL", "user": "Question", "assistant": "Answer"}, "input/output"),
        ({"output": "PRIVATE_SENTINEL", "user": "Question", "assistant": "Answer"}, "input/output"),
        ({"input": "Question", "output": "PRIVATE_SENTINEL", "user": "Question"}, "user/assistant"),
        ({"input": "Question", "output": "PRIVATE_SENTINEL", "assistant": "Answer"}, "user/assistant"),
        ({"input": "Question", "output": "PRIVATE_SENTINEL", "user": None, "assistant": "Answer"}, "user/assistant"),
        ({"input": None, "output": "PRIVATE_SENTINEL", "user": "Question", "assistant": "Answer"}, "input/output"),
        ({"input": "PRIVATE_SENTINEL", "output": "Answer", "user": "Question", "assistant": "Answer"}, "conflicting"),
        ({"input": "Question", "output": "Answer", "user": "Question", "assistant": "Answer "}, "conflicting"),
        ({"input": "PRIVATE_SENTINEL", "assistant": "Answer"}, "input/output"),
        ({"title": "PRIVATE_SENTINEL"}, "input/output"),
    ],
)
def test_corpus_rejects_invalid_text_pairs(tmp_path: Path, fields: dict[str, object], reason: str) -> None:
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('{"input":"Valid","output":"Text"}\n\n' + json.dumps(fields) + "\n", encoding="utf-8")

    with pytest.raises(TagConfigError, match=f"line 3.*{reason}") as error:
        _load_examples(corpus)
    assert "PRIVATE_SENTINEL" not in str(error.value)


BAD_LINES = [
    '{"input":"PRIVATE_SENTINEL","input":"other","output":"text"}',
    '{"input":"x","output":"PRIVATE_SENTINEL","output":"other"}',
    '{"id":"PRIVATE_SENTINEL","id":"other","input":"x","output":"y"}',
    '{"input":"x","output":"y","extra":{"x":1,"x":2}}',
    '{"input":"x","output":"y","extra":NaN}',
    '{"input":"x","output":"y","extra":Infinity}',
    '{"input":"x","output":"y","extra":1e9999}',
    '{"input":"x","output":"y","extra":' + "[" * 1500 + "0" + "]" * 1500 + "}",
    '{"input":"x","output":"y","extra":' + "9" * 5000 + "}",
    '{"input":"PRIVATE_SENTINEL}',
]


@pytest.mark.parametrize(
    "bad",
    BAD_LINES,
    ids=["input", "output", "id", "nested", "nan", "infinity", "overflow", "deep", "integer", "syntax"],
)
def test_invalid_jsonl_stops_before_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(tmp_path / "absent.yaml"))
    store = EpisodeWorkspaceStore(tmp_path)
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(bad + "\n")
    store.write_episode_yaml({"episode_id": "synthetic", "tags": {"examples": str(corpus)}})
    summary_path = store.layout.episode_summary_json_path()
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(EpisodeSummary(summary_markdown="Synthetic").model_dump_json())
    runner = Mock(side_effect=AssertionError("runner must stay unstarted"))
    monkeypatch.setattr("podcast_pipeline.drafter_runner.DrafterCliRunner", runner)
    with pytest.raises(TagConfigError) as error:
        generate_single_asset_candidates_llm(workspace=tmp_path, asset_id="cms_tags")
    assert "line 1" in str(error.value)
    assert "PRIVATE_SENTINEL" not in str(error.value)
    runner.assert_not_called()
    assert not store.layout.copy_candidates_dir.exists()
