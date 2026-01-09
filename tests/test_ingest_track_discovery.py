from __future__ import annotations

from pathlib import Path

import pytest

from podcast_pipeline.entrypoints import ingest


@pytest.mark.parametrize(
    ("stem", "expected_id", "expected_label"),
    [
        ("Ada 1", "ada_01", "Ada 1"),
        ("Ada-02", "ada_02", "Ada 2"),
        ("Ada_03", "ada_03", "Ada 3"),
        ("Ada(4)", "ada_04", "Ada 4"),
        ("Ada [5]", "ada_05", "Ada 5"),
        ("Ada.Babbage-6", "ada_babbage_06", "Ada Babbage 6"),
    ],
)
def test_track_name_heuristics_person_number(
    stem: str,
    expected_id: str,
    expected_label: str,
) -> None:
    path = Path(f"{stem}.flac")
    track_id = ingest._choose_track_id(None, path, set())
    label = ingest._choose_label(None, path)

    assert track_id == expected_id
    assert label == expected_label


def test_track_label_normalizes_separators() -> None:
    path = Path("Foo_Bar.flac")
    label = ingest._choose_label(None, path)

    assert label == "Foo Bar"
