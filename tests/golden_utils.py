from __future__ import annotations

import json
import re
from pathlib import Path

_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def _normalize_json(value: object) -> object:
    if isinstance(value, dict):
        return {str(k): _normalize_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_json(v) for v in value]
    if isinstance(value, str) and _ISO_Z_RE.fullmatch(value) is not None:
        return f"{value[:-1]}+00:00"
    return value


def list_files(root: Path) -> dict[str, Path]:
    return {path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()}


def assert_workspace_matches_golden(*, workspace: Path, golden: Path) -> None:
    workspace_files = list_files(workspace)
    golden_files = list_files(golden)
    workspace_keys = set(workspace_files)
    golden_keys = set(golden_files)
    missing = sorted(golden_keys - workspace_keys)
    extra = sorted(workspace_keys - golden_keys)
    assert not missing and not extra, f"Workspace files differ. Missing: {missing}. Extra: {extra}"

    for relpath in sorted(golden_files):
        workspace_path = workspace_files[relpath]
        golden_path = golden_files[relpath]

        if relpath.endswith(".json"):
            workspace_json = json.loads(workspace_path.read_text(encoding="utf-8"))
            golden_json = json.loads(golden_path.read_text(encoding="utf-8"))
            assert _normalize_json(workspace_json) == _normalize_json(golden_json)
            continue

        workspace_text = workspace_path.read_text(encoding="utf-8")
        golden_text = golden_path.read_text(encoding="utf-8")
        assert workspace_text == golden_text
