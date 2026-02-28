from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from podcast_pipeline.domain.models import Candidate, EpisodeWorkspace
from podcast_pipeline.markdown_html import markdown_to_deterministic_html
from podcast_pipeline.pick_core import (
    build_asset,
    find_candidate_by_id,
    load_candidates,
    load_workspace,
    update_workspace_assets,
    validate_asset_id,
)
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


@dataclass
class BackgroundJob:
    job_id: str
    stage: str
    status: str = "running"
    progress: list[str] = field(default_factory=list)
    error: str | None = None
    result: dict[str, Any] | None = None


class DashboardContext:
    """Shared mutable state for the dashboard web server."""

    def __init__(self, *, workspace: Path) -> None:
        self.workspace = workspace
        self.store = EpisodeWorkspaceStore(workspace)
        self.layout = self.store.layout
        self.lock = threading.Lock()
        self.jobs: dict[str, BackgroundJob] = {}
        self._candidates_by_asset: dict[str, list[Candidate]] | None = None
        self._workspace_state: EpisodeWorkspace | None = None

    def _ensure_candidates(self) -> dict[str, list[Candidate]]:
        if self._candidates_by_asset is None:
            try:
                self._candidates_by_asset = load_candidates(
                    layout=self.layout,
                    asset_id=None,
                )
            except ValueError:
                self._candidates_by_asset = {}
        return self._candidates_by_asset

    def _ensure_workspace_state(self) -> EpisodeWorkspace:
        if self._workspace_state is None:
            self._workspace_state = load_workspace(self.store)
        return self._workspace_state

    def reload_candidates(self) -> None:
        self._candidates_by_asset = None
        self._ensure_candidates()

    def get_status_json(self) -> dict[str, Any]:
        layout = self.layout
        episode_yaml = self._read_episode_yaml_safe()

        transcript_ok = (layout.transcript_dir / "transcript.txt").exists()
        chunk_count = _glob_count(layout.transcript_chunks_dir, "chunk_*.txt")
        summary_ok = layout.episode_summary_json_path().exists()
        candidates = self._ensure_candidates()
        candidate_count = sum(len(v) for v in candidates.values())
        candidate_assets = len(candidates)

        ws = self._ensure_workspace_state()
        selected_count = sum(1 for a in ws.assets if a.selected_candidate_id is not None)

        return {
            "workspace": str(self.workspace.resolve()),
            "episode_id": episode_yaml.get("episode_id", ""),
            "hosts": episode_yaml.get("hosts") or [],
            "stages": {
                "episode_yaml": layout.episode_yaml.exists(),
                "state_json": layout.state_json.exists(),
                "transcript": transcript_ok,
                "chunks": chunk_count,
                "summary": summary_ok,
                "candidates": candidate_count,
                "candidate_assets": candidate_assets,
                "selected": selected_count,
                "total_assets": len(list(candidates.keys())),
            },
        }

    def get_episode_json(self) -> dict[str, Any]:
        data = self._read_episode_yaml_safe()
        return {
            "episode_id": data.get("episode_id", ""),
            "hosts": data.get("hosts") or [],
            "editorial_notes": data.get("editorial_notes") or {},
        }

    def update_episode(self, updates: dict[str, Any]) -> str | None:
        """Apply updates to episode YAML. Returns error message on failure, None on success."""
        data = self._read_episode_yaml_safe()
        if "episode_id" in updates and isinstance(updates["episode_id"], str) and updates["episode_id"].strip():
            data["episode_id"] = updates["episode_id"]
        if "hosts" in updates and isinstance(updates["hosts"], list):
            hosts = [h for h in updates["hosts"] if isinstance(h, str)]
            data["hosts"] = hosts
        if "editorial_notes" in updates and isinstance(updates["editorial_notes"], dict):
            existing_notes = data.get("editorial_notes") or {}
            if not isinstance(existing_notes, dict):
                existing_notes = {}
            for k, v in updates["editorial_notes"].items():
                if isinstance(k, str) and isinstance(v, str):
                    existing_notes[k] = v
            # Remove empty notes
            existing_notes = {k: v for k, v in existing_notes.items() if v}
            data["editorial_notes"] = existing_notes or None
        # episode_id is required by the schema — refuse to write without one
        eid = data.get("episode_id")
        if not isinstance(eid, str) or not eid.strip():
            return "episode_id is required"
        self.store.write_episode_yaml(data)
        return None

    def get_assets_json(self) -> list[dict[str, Any]]:
        candidates = self._ensure_candidates()
        ws = self._ensure_workspace_state()
        assets_by_id = {asset.asset_id: asset for asset in ws.assets}

        result: list[dict[str, Any]] = []
        for asset_key in sorted(candidates):
            cands = candidates[asset_key]
            existing = assets_by_id.get(asset_key)
            selected_id = str(existing.selected_candidate_id) if existing and existing.selected_candidate_id else None

            candidate_items: list[dict[str, Any]] = []
            for c in cands:
                candidate_items.append(
                    {
                        "candidate_id": str(c.candidate_id),
                        "content": c.content,
                        "content_html": markdown_to_deterministic_html(c.content),
                        "format": c.format.value,
                    }
                )

            result.append(
                {
                    "asset_id": asset_key,
                    "selected_candidate_id": selected_id,
                    "candidates": candidate_items,
                }
            )
        return result

    def select_candidate(self, asset_id: str, candidate_id_str: str) -> str | None:
        """Select a candidate. Returns error message on failure, None on success."""
        try:
            validate_asset_id(asset_id)
        except ValueError as exc:
            return str(exc)

        candidates = self._ensure_candidates()
        if asset_id not in candidates:
            return f"Unknown asset_id: {asset_id}"

        try:
            from uuid import UUID

            candidate_uuid = UUID(candidate_id_str)
        except ValueError:
            return f"Invalid candidate_id: {candidate_id_str}"

        asset_candidates = candidates[asset_id]
        match = find_candidate_by_id(asset_candidates, candidate_uuid)
        if match is None:
            return f"candidate_id {candidate_id_str} not found for asset {asset_id}"

        ws = self._ensure_workspace_state()
        assets_by_id = {asset.asset_id: asset for asset in ws.assets}
        existing = assets_by_id.get(asset_id)

        self.store.write_selected_text(asset_id, match.format, match.content)
        assets_by_id[asset_id] = build_asset(
            asset_id=asset_id,
            existing=existing,
            candidates=asset_candidates,
            selected_candidate_id=match.candidate_id,
        )
        self._workspace_state = update_workspace_assets(ws, assets_by_id)
        self.store.write_state(self._workspace_state)
        return None

    def get_editorial_notes(self, asset_id: str) -> str:
        data = self._read_episode_yaml_safe()
        notes = data.get("editorial_notes") or {}
        if isinstance(notes, dict):
            value = notes.get(asset_id, "")
            return str(value) if value else ""
        return ""

    def set_editorial_notes(self, asset_id: str, notes: str) -> None:
        data = self._read_episode_yaml_safe()
        editorial = data.get("editorial_notes") or {}
        if not isinstance(editorial, dict):
            editorial = {}
        if notes:
            editorial[asset_id] = notes
        else:
            editorial.pop(asset_id, None)
        data["editorial_notes"] = editorial or None
        self.store.write_episode_yaml(data)

    def clear_editorial_notes(self, asset_id: str) -> None:
        self.set_editorial_notes(asset_id, "")

    def create_job(self, stage: str) -> BackgroundJob:
        job_id = str(uuid.uuid4())[:8]
        job = BackgroundJob(job_id=job_id, stage=stage)
        self.jobs[job_id] = job
        return job

    def _read_episode_yaml_safe(self) -> dict[str, Any]:
        if not self.layout.episode_yaml.exists():
            return {}
        try:
            return self.store.read_episode_yaml()
        except Exception:
            return {}


def _glob_count(path: Path, pattern: str) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.glob(pattern))
