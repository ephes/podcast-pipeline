"""Bound untrusted human-readable terminal fields without changing stored text."""

from __future__ import annotations

import unicodedata


def terminal_text(value: str, *, limit: int = 1000) -> str:
    parts: list[str] = []
    size = 0
    for char in value:
        number = ord(char)
        if char in "\n\r\t":
            escaped = {"\n": r"\n", "\r": r"\r", "\t": r"\t"}[char]
        elif number < 32 or 127 <= number <= 159:
            escaped = f"\\x{number:02x}"
        elif unicodedata.category(char) == "Cf":
            escaped = f"\\u{number:04x}"
        else:
            escaped = char
        if size + len(escaped) > limit - 3:
            return "".join(parts) + "..."
        parts.append(escaped)
        size += len(escaped)
    return "".join(parts)
