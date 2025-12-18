from __future__ import annotations

import re
from pathlib import Path

import pytest

_TIMECODE_RE = re.compile(r"^(?P<m>\d{2}):(?P<s>\d{2})\s+")


def _fixture_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "pp_068"


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_chapters(chapters_raw: str) -> list[tuple[int, str]]:
    chapters: list[tuple[int, str]] = []
    for line in chapters_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _TIMECODE_RE.match(line)
        if match is None:
            raise ValueError(f"Invalid chapter line (expected MM:SS ...): {line!r}")
        minutes = int(match.group("m"))
        seconds = int(match.group("s"))
        title = line[match.end() :].strip()
        if not title:
            raise ValueError("Chapter title must be non-empty")
        chapters.append((minutes * 60 + seconds, title))
    return chapters


def test_fixture_transcript_exists_is_small_and_has_timecodes() -> None:
    path = _fixture_dir() / "transcript.txt"
    raw = _load_text(path)

    assert raw, "transcript fixture must be non-empty"
    assert raw.endswith("\n"), "transcript fixture should end with a newline"
    assert len(raw.encode("utf-8")) < 20_000, "transcript fixture must remain small and stable"
    assert raw.count("00:") >= 3, "transcript fixture should contain multiple timecoded lines"


def test_fixture_chapters_exists_is_small_and_parses_monotonic() -> None:
    path = _fixture_dir() / "chapters.txt"
    raw = _load_text(path)

    assert raw, "chapters fixture must be non-empty"
    assert raw.endswith("\n"), "chapters fixture should end with a newline"
    assert len(raw.encode("utf-8")) < 5_000, "chapters fixture must remain small and stable"

    chapters = _parse_chapters(raw)
    assert len(chapters) >= 3

    times = [t for (t, _) in chapters]
    assert times == sorted(times)
    assert len(times) == len(set(times))


def test_fixture_chapters_timecodes_are_mmss() -> None:
    raw = _load_text(_fixture_dir() / "chapters.txt")
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        assert _TIMECODE_RE.match(line) is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("00:00 Intro\n", [(0, "Intro")]),
        ("00:05 A\n00:10 B\n", [(5, "A"), (10, "B")]),
    ],
)
def test_parse_chapters_smoke(raw: str, expected: list[tuple[int, str]]) -> None:
    assert _parse_chapters(raw) == expected
