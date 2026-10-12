from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, NoReturn

import pytest

from podcast_pipeline import cms_examples, rss_examples, workspace_store


@pytest.fixture(params=["cms", "rss"])
def export_case(request: pytest.FixtureRequest) -> tuple[Any, list[Any]]:
    if request.param == "cms":
        examples = cms_examples.parse_cms_examples(
            [{"title": title, "description": "<p>Grüße</p>"} for title in ("One", "Two")],
            api_url="https://example.test/api",
            limit=2,
        )
        return cms_examples.write_cms_examples_jsonl, examples
    rss = (
        "<rss><channel>"
        + "".join(f"<item><title>{title}</title><description>Grüße</description></item>" for title in ("One", "Two"))
        + "</channel></rss>"
    )
    return rss_examples.write_rss_examples_jsonl, rss_examples.parse_rss_examples(
        rss, feed_url="https://example.test/feed", limit=2
    )


def test_export_preserves_corpus_on_second_serialization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, export_case: tuple[Any, list[Any]]
) -> None:
    writer, examples = export_case
    output = tmp_path / "examples.jsonl"
    output.write_bytes(b"previous corpus\n")
    original = type(examples[0]).to_record
    calls = 0

    def to_record(self: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        record: dict[str, Any] = original(self)
        if calls == 2:
            record["invalid"] = object()
        return record

    monkeypatch.setattr(type(examples[0]), "to_record", to_record)
    with pytest.raises(TypeError):
        writer(examples=examples, output_path=output)
    assert calls == 2
    assert output.read_bytes() == b"previous corpus\n"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_export_preserves_corpus_on_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, export_case: tuple[Any, list[Any]], failure: str
) -> None:
    writer, examples = export_case
    output = tmp_path / "examples.jsonl"
    output.write_bytes(b"previous corpus\n")
    original_temp = workspace_store.tempfile.NamedTemporaryFile
    injected = False

    def fail(*args: Any, **kwargs: Any) -> NoReturn:
        nonlocal injected
        injected = True
        raise OSError("injected I/O failure")

    def temporary_file(*args: Any, **kwargs: Any) -> Any:
        tmp = original_temp(*args, **kwargs)
        original_write = tmp.write

        def partial_write(data: bytes) -> int:
            original_write(data[:5])
            fail()

        monkeypatch.setattr(tmp, "write", partial_write)
        return tmp

    if failure == "write":
        monkeypatch.setattr(workspace_store.tempfile, "NamedTemporaryFile", temporary_file)
    else:
        monkeypatch.setattr(workspace_store.os, failure, fail)
    with pytest.raises(OSError, match="injected I/O failure"):
        writer(examples=examples, output_path=output)
    assert injected
    assert output.read_bytes() == b"previous corpus\n"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("empty", [False, True])
def test_export_publishes_complete_deterministic_utf8(
    tmp_path: Path, export_case: tuple[Any, list[Any]], empty: bool
) -> None:
    writer, examples = export_case
    if empty:
        examples = []
    output = tmp_path / "nested" / "examples.jsonl"
    expected = "".join(
        json.dumps(example.to_record(), ensure_ascii=False, sort_keys=True) + "\n" for example in examples
    )
    for _ in range(2):
        writer(examples=examples, output_path=output)
        assert output.read_bytes() == expected.encode("utf-8")
        assert list(output.parent.iterdir()) == [output]


@pytest.mark.parametrize("mode", [None, 0o640, 0o644])
def test_export_permissions(tmp_path: Path, export_case: tuple[Any, list[Any]], mode: int | None) -> None:
    writer, examples = export_case
    output = tmp_path / "examples.jsonl"
    if mode is not None:
        output.write_text("previous corpus\n")
        output.chmod(mode)
    writer(examples=examples, output_path=output)
    assert stat.S_IMODE(output.stat().st_mode) == (0o600 if mode is None else mode)
