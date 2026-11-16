"""Bounded reads of a single regular UTF-8 file generation."""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack
from pathlib import Path


def _generation(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def read_regular_utf8(path: Path, *, max_bytes: int, dir_fd: int | None = None) -> str:
    named = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ValueError("Source must be a regular file within the byte limit")
        if _generation(named) != _generation(before):
            raise ValueError("Source changed before open")
        parts: list[bytes] = []
        size = 0
        while part := os.read(fd, min(64 * 1024, max_bytes + 1 - size)):
            size += len(part)
            if size > max_bytes:
                raise ValueError("Source byte limit exceeded")
            parts.append(part)
        after = os.fstat(fd)
        current = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if (
            _generation(before) != _generation(after)
            or _generation(before) != _generation(current)
            or size != before.st_size
        ):
            raise ValueError("Source changed during read")
        return b"".join(parts).decode("utf-8")
    finally:
        os.close(fd)


def read_workspace_utf8(root: Path, reference: Path, *, max_bytes: int) -> str:
    """Anchor each component to owned directory descriptors, refusing symlinks/swaps."""
    if reference.is_absolute() or ".." in reference.parts or not reference.parts:
        raise ValueError("Expected a relative workspace file")
    root = root.resolve(strict=True)
    with ExitStack() as stack:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        parent = os.open(root, flags)
        stack.callback(os.close, parent)
        directories: list[tuple[int | None, Path, int]] = [(None, root, parent)]
        for part in reference.parts[:-1]:
            child = os.open(part, flags, dir_fd=parent)
            stack.callback(os.close, child)
            directories.append((parent, Path(part), child))
            parent = child
        text = read_regular_utf8(Path(reference.name), max_bytes=max_bytes, dir_fd=parent)
        for containing, name, fd in directories:
            named = os.stat(name, dir_fd=containing, follow_symlinks=False)
            opened = os.fstat(fd)
            if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("Workspace directory changed during read")
        return text
