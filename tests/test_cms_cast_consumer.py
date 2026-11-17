"""Optional real-consumer check; see docs/decisions/cms-draft.md for setup."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from podcast_pipeline.cms_draft import format_chaptermarks


def test_chapters_through_django_cast_develop() -> None:
    checkout = os.environ.get("DJANGO_CAST_CHECKOUT")
    if not checkout:
        pytest.skip("set DJANGO_CAST_CHECKOUT to a supported django-cast/develop checkout")
    root = Path(checkout).resolve()
    chapters = [(0.2, "First"), (0.7, "Second"), (1.000001, "Third"), (86399.999999, "Last")]
    result = subprocess.run(
        [
            str(root / ".venv/bin/python"),
            "-c",
            """
import json
import os
import sys
os.environ["DJANGO_SETTINGS_MODULE"] = "tests.settings"
import django
django.setup()
from cast.forms import ChapterMarksField
marks = ChapterMarksField().clean(sys.stdin.read())
print(json.dumps([[mark.start.isoformat(), mark.title] for mark in marks]))
""",
        ],
        cwd=root,
        input=format_chaptermarks(chapters),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [
        ["00:00:00.200000", "First"],
        ["00:00:00.700000", "Second"],
        ["00:00:01.000001", "Third"],
        ["23:59:59.999999", "Last"],
    ]
