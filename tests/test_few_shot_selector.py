from __future__ import annotations

from podcast_pipeline.few_shot_selector import select_few_shot_examples


def test_select_few_shot_examples_bounded_and_relevant() -> None:
    examples = [
        {"example_id": "one", "input": "Input A", "output": "Output A", "tags": ["ai", "ml"]},
        {"example_id": "two", "input": "Input B", "output": "Output B", "tags": ["gardening"]},
        {"example_id": "three", "input": "Input C", "output": "Output C", "tags": ["ai"]},
    ]

    selected = select_few_shot_examples(examples=examples, topics={"AI"}, limit=2)

    assert len(selected) == 2
    assert selected[0].input_text == "Input A"
    assert selected[1].input_text == "Input C"


def test_select_few_shot_examples_falls_back_to_order() -> None:
    examples = [
        {"example_id": "first", "input": "Input 1", "output": "Output 1"},
        {"example_id": "second", "input": "Input 2", "output": "Output 2"},
    ]

    selected = select_few_shot_examples(examples=examples, topics=[], limit=1)

    assert len(selected) == 1
    assert selected[0].input_text == "Input 1"


def test_select_few_shot_examples_deduplicates_ids() -> None:
    examples = [
        {"example_id": "dup", "input": "Input A", "output": "Output A", "tags": ["ai"]},
        {"example_id": "dup", "input": "Input B", "output": "Output B", "tags": ["ai"]},
        {"example_id": "unique", "input": "Input C", "output": "Output C", "tags": ["ai"]},
    ]

    selected = select_few_shot_examples(examples=examples, topics={"ai"}, limit=2)

    assert [example.input_text for example in selected] == ["Input A", "Input C"]


def test_empty_topics_fill_limit_with_unique_ids() -> None:
    for topics in ([], ["", "   "]):
        examples = [
            {"id": "dup", "input": "first", "output": "one"},
            {"id": "dup", "input": "second", "output": "two"},
            {"input": "generated", "output": "three"},
            {"input": "generated", "output": "three"},
            {"id": "last", "input": "last", "output": "four"},
        ]
        selected = select_few_shot_examples(examples=examples, topics=topics, limit=3)
        assert [example.input_text for example in selected] == ["first", "generated", "last"]


def test_unicode_words_match_without_fragments() -> None:
    from podcast_pipeline.few_shot_selector import FewShotExampleRecord, _tokens_from_values

    for word in ("für", "Überprüfung", "Möglichkeiten", "fu\u0308r", "ÜBERPRÜFUNG"):
        tokens = _tokens_from_values([word])
        assert len(tokens) == 1
        unrelated = FewShotExampleRecord("fragment", "r", "output")
        assert unrelated.match_score(tokens) == 0
        matching = FewShotExampleRecord("match", "für Überprüfung Möglichkeiten", "output")
        assert matching.match_score(tokens) == 1
        tagged = FewShotExampleRecord("tag", "unrelated", "output", tags=(word,))
        assert tagged.match_score(tokens) == 3
        selected = select_few_shot_examples(examples=[unrelated, matching], topics=[word], limit=1)
        assert selected[0].input_text == matching.input_text


def test_english_numeric_words_and_casefolding() -> None:
    from podcast_pipeline.few_shot_selector import _tokens_from_values

    assert _tokens_from_values(["AI Python_3 123 STRASSE Straße"]) == {"ai", "python", "3", "123", "strasse"}
