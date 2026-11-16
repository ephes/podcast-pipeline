"""Strict, bounded JSON control documents, admitted before object hydration."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from podcast_pipeline.review_loop_engine import unique_json_members

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_TOKENS = 100_000  # Container starts, member names and scalar values.
MAX_JSON_NUMBER_CHARS = 256
_JSON_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\]]|[^\s,:{}\[\]"]+')


def _admit_structure(raw: str) -> None:
    if len(raw) > MAX_JSON_BYTES or len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("JSON byte limit")
    depth = tokens = 0
    for match in _JSON_TOKEN.finditer(raw):
        token = match.group()
        if token in ("}", "]"):
            depth -= 1
            continue
        tokens += 1
        if tokens > MAX_JSON_TOKENS:
            raise ValueError("JSON token limit")
        if token in ("{", "["):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON depth limit")
        elif token.startswith('"'):
            # Escaped surrogate code points must not survive hydration.
            continue
        elif len(token) > MAX_JSON_NUMBER_CHARS:
            raise ValueError("JSON scalar limit")


def _invalid_constant(value: str) -> Any:
    raise ValueError("JSON nonstandard constant")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON nonfinite number")
    return number


def load_control_json(raw: str) -> Any:
    """Raise a content-free ValueError for invalid or excessive control JSON."""
    try:
        _admit_structure(raw)
        value = json.loads(
            raw,
            object_pairs_hook=unique_json_members,
            parse_constant=_invalid_constant,
            parse_float=_finite_float,
        )
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("Invalid or excessive control JSON") from None
