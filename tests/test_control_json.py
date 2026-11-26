from __future__ import annotations

from collections.abc import Callable
from typing import SupportsIndex

import pytest

from podcast_pipeline import control_json


def test_utf8_admission_does_not_reencode_the_whole_document(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded_lengths: list[int] = []

    class TrackedText(str):
        def __getitem__(self, index: SupportsIndex | slice) -> str:
            return TrackedText(super().__getitem__(index))

        def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
            encoded_lengths.append(len(self))
            return super().encode(encoding, errors)

    class TrackedBytes(bytes):
        def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
            return TrackedText(super().decode(encoding, errors))

    class TrackedBytearray(bytearray):
        def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
            return TrackedText(super().decode(encoding, errors))

    value = "aé€😀" * 5000
    document = f'["{value}"]'
    encoded = document.encode("utf-8")
    monkeypatch.setattr(control_json, "MAX_JSON_BYTES", len(encoded))

    for wire_type in (TrackedBytes, TrackedBytearray):
        assert control_json.load_control_json(wire_type(encoded)) == [value]
        with pytest.raises(ValueError, match="Invalid or excessive control JSON"):
            control_json.load_control_json(wire_type(encoded + b" "))
    assert encoded_lengths == []

    assert control_json.load_control_json(TrackedText(document)) == [value]
    with pytest.raises(ValueError, match="Invalid or excessive control JSON"):
        control_json.load_control_json(TrackedText(document + " "))
    assert max(encoded_lengths, default=0) < len(document)


@pytest.mark.parametrize("as_input", [str, lambda text: text.encode(), lambda text: bytearray(text, "utf-8")])
def test_control_json_rejects_escaped_unpaired_surrogate(as_input: Callable[[str], str | bytes | bytearray]) -> None:
    with pytest.raises(ValueError, match="Invalid or excessive control JSON"):
        control_json.load_control_json(as_input(r'{"\ud800": 1}'))
    assert control_json.load_control_json(as_input(r'"\ud83d\ude00"')) == "😀"


def test_control_json_rejects_literal_surrogate_and_invalid_utf8() -> None:
    for raw in ('"\ud800"', b'"\xed\xa0\x80"', bytearray(b'"\xed\xa0\x80"')):
        with pytest.raises(ValueError, match="Invalid or excessive control JSON"):
            control_json.load_control_json(raw)
