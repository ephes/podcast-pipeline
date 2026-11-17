"""Chapter text is a microsecond-resolution django-cast TimeField boundary."""

from datetime import time

import pytest

from podcast_pipeline.cms_draft import CmsConfigError, format_chaptermarks


def test_fractional_chapter_starts_retain_supported_precision() -> None:
    text = format_chaptermarks([(0.2, "First"), (0.7, "Second"), (1.000001, "Third"), (3600, "Hour")])
    assert text == "00:00:00.200000 First\n00:00:00.700000 Second\n00:00:01.000001 Third\n01:00:00 Hour"
    assert [time.fromisoformat(line.split()[0]) for line in text.splitlines()] == [
        time(microsecond=200000),
        time(microsecond=700000),
        time(second=1, microsecond=1),
        time(hour=1),
    ]


def test_fractional_chapter_rounding_and_carry() -> None:
    assert format_chaptermarks([(0.0000006, "Tiny"), (59.9999996, "Minute")]) == (
        "00:00:00.000001 Tiny\n00:01:00 Minute"
    )


def test_fractional_chapter_rounding_collision_is_refused() -> None:
    with pytest.raises(CmsConfigError, match="chapter.*microsecond"):
        format_chaptermarks([(0.0000001, "First"), (0.0000002, "Second")])
