import json
from pathlib import Path
from typing import Any, NoReturn

import pytest

from podcast_pipeline import workspace_store
from podcast_pipeline.few_shot_selector import load_few_shot_records, select_few_shot_records
from podcast_pipeline.prompting import PromptRenderer, default_prompt_registry
from podcast_pipeline.rss_examples import parse_rss_examples, write_rss_examples_jsonl
from podcast_pipeline.summarization_llm import summarize_transcript_chunks_llm
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


def test_same_local_guid_in_independent_feeds_selects_both(tmp_path: Path) -> None:
    xml = (
        "<rss><channel><item><title>One</title><guid>episode-1</guid>"
        "<description>{}</description></item></channel></rss>"
    )
    first = parse_rss_examples(xml.format("First"), feed_url="https://ONE.test:443/rss", limit=1)[0]
    repeat = parse_rss_examples(xml.format("First"), feed_url="https://u:p@one.test/rss?token=secret#frag", limit=1)[0]
    second = parse_rss_examples(xml.format("Second"), feed_url="https://two.test/rss", limit=1)[0]
    assert first.example_id() == repeat.example_id()
    assert first.example_id() != second.example_id()
    corpus = tmp_path / "corpus.jsonl"
    write_rss_examples_jsonl(examples=[first, second], output_path=corpus)
    selected = select_few_shot_records(examples=load_few_shot_records(corpus), topics=[], limit=2)
    assert [record.output_text for record in selected] == ["First", "Second"]


class Runner:
    def run(self, prompt_text: str) -> dict[str, Any]:
        return {"summary_markdown": "New summary"}


@pytest.mark.parametrize("failure", ["write", "flush", "fsync", "replace", "interrupt"])
def test_chunk_cache_keeps_old_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    source = layout.transcript_chunk_text_path(1)
    source.parent.mkdir(parents=True)
    source.write_text("Synthetic transcript")
    cache = layout.chunk_summary_json_path(1)
    cache.parent.mkdir(parents=True)
    previous = '{"version":1,"chunk_id":1,"summary_markdown":"Previous summary"}\n'
    cache.write_text(previous)
    original_temp = workspace_store.tempfile.NamedTemporaryFile
    injected = False

    def fail(*args: Any, **kwargs: Any) -> NoReturn:
        nonlocal injected
        injected = True
        if failure == "interrupt":
            raise KeyboardInterrupt
        raise OSError("injected publication failure")

    def temporary_file(*args: Any, **kwargs: Any) -> Any:
        tmp = original_temp(*args, **kwargs)
        original_write = tmp.write

        def partial(data: bytes) -> int:
            original_write(data[:5])
            fail()

        monkeypatch.setattr(tmp, "flush" if failure == "flush" else "write", fail if failure == "flush" else partial)
        return tmp

    with monkeypatch.context() as patch:
        if failure in {"write", "flush", "interrupt"}:
            patch.setattr(workspace_store.tempfile, "NamedTemporaryFile", temporary_file)
        else:
            patch.setattr(workspace_store.os, failure, fail)
        with pytest.raises(KeyboardInterrupt if failure == "interrupt" else OSError):
            summarize_transcript_chunks_llm(
                layout=layout, chunk_ids=[1], runner=Runner(), renderer=PromptRenderer(default_prompt_registry())
            )
    assert injected
    assert cache.read_text() == previous
    assert list(cache.parent.iterdir()) == [cache]
    summarize_transcript_chunks_llm(
        layout=layout, chunk_ids=[1], runner=Runner(), renderer=PromptRenderer(default_prompt_registry())
    )
    assert json.loads(cache.read_text())["summary_markdown"] == "New summary"
