from __future__ import annotations

from podcast_pipeline.tag_parsing import normalize_tag_values, parse_tag_list


def test_parse_tag_list_prefers_bullets() -> None:
    text = "# Tags\n\n- Python\n- LLM\nNot used line\n"
    assert parse_tag_list(text) == ["Python", "LLM"]


def test_parse_tag_list_supports_common_separators() -> None:
    assert parse_tag_list("python, llm; devops | django") == ["python", "llm", "devops", "django"]


def test_parse_tag_list_keeps_separator_free_lines_in_mixed_input() -> None:
    assert parse_tag_list("python, llm\ndevops") == ["python", "llm", "devops"]


def test_parse_tag_list_space_blob_threshold() -> None:
    short_line = "python llm devops"
    assert parse_tag_list(short_line) == [short_line]

    long_line = "python llm agentic coding devops django postgres claude gemini"
    assert parse_tag_list(long_line) == [
        "python",
        "llm",
        "agentic",
        "coding",
        "devops",
        "django",
        "postgres",
        "claude",
        "gemini",
    ]


def test_normalize_tag_values_uses_casefold() -> None:
    assert normalize_tag_values(["Straße", "strasse", "LLM", "llm"]) == ["Straße", "LLM"]
