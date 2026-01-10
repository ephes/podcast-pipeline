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
