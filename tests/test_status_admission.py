from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from podcast_pipeline.domain.models import Candidate, TextFormat
from podcast_pipeline.entrypoints import status
from podcast_pipeline.entrypoints.cli import app


@pytest.mark.parametrize("relative", ["state.json", "copy/protocol/description/state.json"])
@pytest.mark.parametrize("damage", ["utf8", "oversize", "depth", "fifo"])
def test_status_refuses_unusable_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str, damage: str
) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(status, "MAX_STATUS_FILE_BYTES", 1024, raising=False)
    if damage == "fifo":
        os.mkfifo(path)
        original = Path.read_text

        def guarded_read(self: Path, *args: object, **kwargs: object) -> str:
            assert self != path, "status must not use blocking read_text on a FIFO"
            return original(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", guarded_read)
    else:
        path.write_bytes(
            {
                "utf8": b"\xff",
                "oversize": b'{"episode_id":"synthetic","root_dir":"."}' + b" " * 1025,
                "depth": b"[" * 70 + b"]" * 70,
            }[damage]
        )
    before = path.lstat()
    result = CliRunner().invoke(app, ["status", "--workspace", str(tmp_path)])
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
    assert "invalid" in result.output.lower() or "incomplete" in result.output.lower()
    assert "review convergence: ok" not in result.output
    assert path.lstat() == before


def test_status_stops_inventory_at_total_entry_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = tmp_path / "transcript/chunks"
    chunks.mkdir(parents=True)
    for number in range(40):
        (chunks / f"ignored_{number}").touch()
    monkeypatch.setattr(status, "MAX_STATUS_ENTRIES", 8, raising=False)
    visited: list[Path] = []
    original = os.scandir

    @contextmanager
    def counted(path: Any) -> Iterator[Iterator[os.DirEntry[str]]]:
        with original(path) as entries:

            def children() -> Iterator[os.DirEntry[str]]:
                for child in entries:
                    visited.append(Path(child.path))
                    yield child

            yield children()

    monkeypatch.setattr(os, "scandir", counted)
    result = CliRunner().invoke(app, ["status", "--workspace", str(tmp_path)])
    assert "incomplete" in result.output.lower()
    assert len(visited) <= 9
    assert "Checklist:" not in result.output


def test_status_charges_reads_to_one_byte_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "state.json").write_text('{"episode_id":"synthetic","root_dir":"."}')
    monkeypatch.setattr(status, "MAX_STATUS_TOTAL_BYTES", 16, raising=False)
    result = CliRunner().invoke(app, ["status", "--workspace", str(tmp_path)])
    assert "incomplete" in result.output.lower()
    assert "Checklist:" not in result.output


def test_status_charges_refused_read_by_file_size(tmp_path: Path) -> None:
    path = tmp_path / "copy/selected/description.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")
    inspection = status._Inspection(status.EpisodeWorkspaceLayout(root=tmp_path))
    with pytest.raises(ValueError):
        inspection.read_text(path)
    assert inspection.bytes_read == 1


@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo", "symlink-parent"])
def test_status_counts_only_regular_workspace_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(status, "_required_asset_ids", lambda: ("description",))
    chunks = tmp_path / "transcript/chunks"
    candidate_dir = tmp_path / "copy/candidates/description"
    for directory in (chunks, candidate_dir):
        directory.mkdir(parents=True)
    chunk = chunks / "chunk_0001.txt"
    evidence = Candidate(asset_id="description", format=TextFormat.markdown, content="Synthetic candidate")
    candidate = candidate_dir / f"candidate_{evidence.candidate_id}.json"
    contents = {chunk: "Synthetic transcript chunk", candidate: evidence.model_dump_json()}
    target = tmp_path / "target"
    target.write_text(evidence.model_dump_json())
    for path, content in contents.items():
        if kind == "directory":
            path.mkdir()
        elif kind == "symlink":
            path.symlink_to(target)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            moved = path.parent.with_name(path.parent.name + "-moved")
            path.parent.rename(moved)
            (moved / path.name).write_text(content)
            path.parent.symlink_to(moved, target_is_directory=True)

    runner = CliRunner()
    args = ["status", "--workspace", str(tmp_path)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "transcript/chunks/*.txt: missing" in result.output
    assert "copy/candidates: missing for description" in result.output
    assert "blocked (no candidates): description" in result.output

    for path, content in contents.items():
        if kind == "directory":
            path.rmdir()
        elif kind == "symlink-parent":
            moved = path.parent.readlink()
            path.parent.unlink()
            moved.rename(path.parent)
        else:
            path.unlink()
        path.write_text(content)
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "transcript/chunks/*.txt: ok (1 file(s))" in result.output
    assert "copy/candidates: ok" in result.output
    assert "needs pick: description" in result.output
