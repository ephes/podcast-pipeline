from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from podcast_pipeline.domain.models import (
    Asset,
    AssetKind,
    Candidate,
    EpisodeWorkspace,
    ProvenanceRef,
    TextFormat,
)
from podcast_pipeline.tag_assets import (
    TAG_ASSET_IDS,
    TagSettings,
    build_tag_pool,
    render_selected_tags_markdown,
    tag_cap_error,
)
from podcast_pipeline.tag_parsing import normalize_tag_values, parse_tag_list
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore

MANUAL_SELECTION_PROVENANCE_KIND = "manual_selection"

# Short single-value assets are stored as plain text; everything else as markdown
# (which also publishes the derived ``.html`` next to it).
_PLAIN_SELECTION_ASSETS = frozenset(
    {
        AssetKind.slug.value,
        AssetKind.title_detail.value,
        AssetKind.title_seo.value,
        AssetKind.subtitle_auphonic.value,
        AssetKind.summary_short.value,
    }
)
_SINGLE_LINE_ASSETS = frozenset(
    {
        AssetKind.slug.value,
        AssetKind.title_detail.value,
        AssetKind.title_seo.value,
        AssetKind.subtitle_auphonic.value,
    }
)
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NON_SLUG_CHARS_RE = re.compile(r"[^a-z0-9]+")
_GERMAN_TRANSLITERATIONS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def load_candidates(
    *,
    layout: EpisodeWorkspaceLayout,
    asset_id: str | None,
) -> dict[str, list[Candidate]]:
    """Load candidate JSON files from workspace, optionally filtered by asset_id."""
    root = layout.copy_candidates_dir
    if not root.exists():
        raise ValueError(f"Missing candidates directory: {root}")

    asset_dirs: list[Path]
    if asset_id is not None:
        validate_asset_id(asset_id)
        asset_dir = root / asset_id
        if not asset_dir.exists():
            raise ValueError(f"Missing candidates directory for asset {asset_id}: {asset_dir}")
        asset_dirs = [asset_dir]
    else:
        asset_dirs = [path for path in sorted(root.iterdir()) if path.is_dir()]

    candidates_by_asset: dict[str, list[Candidate]] = {}
    for asset_dir in asset_dirs:
        candidates = _load_candidates_from_dir(asset_dir)
        if candidates:
            candidates_by_asset[asset_dir.name] = candidates

    if asset_id is not None and asset_id not in candidates_by_asset:
        raise ValueError(f"No candidates found for asset {asset_id}")
    return candidates_by_asset


def _load_candidates_from_dir(path: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    for candidate_path in sorted(path.glob("candidate_*.json")):
        try:
            raw = json.loads(candidate_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON at {candidate_path}: {exc}") from exc
        try:
            candidate = Candidate.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"Invalid candidate schema at {candidate_path}: {exc}") from exc
        if candidate.asset_id != path.name:
            raise ValueError(f"candidate asset_id mismatch: {candidate_path}")
        candidates.append(candidate)
    candidates.sort(key=lambda item: (item.created_at, str(item.candidate_id)))
    return candidates


def load_workspace(store: EpisodeWorkspaceStore) -> EpisodeWorkspace:
    """Load workspace state, creating a default if state.json doesn't exist."""
    if store.layout.state_json.exists():
        return store.read_state()
    episode_id = _episode_id_from_yaml(store)
    return EpisodeWorkspace(episode_id=episode_id, root_dir=".")


def _episode_id_from_yaml(store: EpisodeWorkspaceStore) -> str:
    if store.layout.episode_yaml.exists():
        data = store.read_episode_yaml()
        episode_id = data.get("episode_id")
        if isinstance(episode_id, str) and episode_id.strip():
            return episode_id
    return store.layout.root.name


def build_asset(
    *,
    asset_id: str,
    existing: Asset | None,
    candidates: list[Candidate],
    selected_candidate_id: UUID | None,
) -> Asset:
    """Build an Asset model with merged candidates and the selected candidate id.

    ``selected_candidate_id`` is ``None`` for a custom (manually written) selection.
    """
    merged_candidates = _merge_candidates(existing, candidates)
    reviews = list(existing.reviews) if existing else []
    kind = existing.kind if existing and existing.kind is not None else _asset_kind(asset_id)
    return Asset(
        asset_id=asset_id,
        kind=kind,
        candidates=merged_candidates,
        reviews=reviews,
        selected_candidate_id=selected_candidate_id,
    )


def _merge_candidates(existing: Asset | None, candidates: list[Candidate]) -> list[Candidate]:
    by_id: dict[UUID, Candidate] = {}
    if existing is not None:
        for candidate in existing.candidates:
            by_id[candidate.candidate_id] = candidate
    for candidate in candidates:
        by_id[candidate.candidate_id] = candidate

    merged: list[Candidate] = []
    seen: set[UUID] = set()
    for candidate in candidates:
        merged.append(by_id[candidate.candidate_id])
        seen.add(candidate.candidate_id)
    if existing is not None:
        for candidate in existing.candidates:
            if candidate.candidate_id not in seen:
                merged.append(candidate)
    return merged


def update_workspace_assets(
    workspace: EpisodeWorkspace,
    assets_by_id: dict[str, Asset],
) -> EpisodeWorkspace:
    """Return a copy of workspace with merged/updated assets."""
    updated_assets = _merge_assets(workspace.assets, assets_by_id)
    return workspace.model_copy(update={"assets": updated_assets})


def _merge_assets(existing: list[Asset], assets_by_id: dict[str, Asset]) -> list[Asset]:
    out: list[Asset] = []
    seen: set[str] = set()
    for asset in existing:
        replacement = assets_by_id.get(asset.asset_id)
        if replacement is not None:
            out.append(replacement)
        else:
            out.append(asset)
        seen.add(asset.asset_id)
    for asset_id_key in sorted(assets_by_id):
        if asset_id_key not in seen:
            out.append(assets_by_id[asset_id_key])
    return out


def tag_pool_json(
    candidates: Sequence[Candidate],
    *,
    settings: TagSettings,
    hosts: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """Serialize the merged tag suggestion pool of one tag asset."""
    pool = build_tag_pool((c.content for c in candidates), settings=settings, hosts=hosts)
    return [{"tag": entry.tag, "count": entry.count} for entry in pool]


def write_selected_tags(
    *,
    store: EpisodeWorkspaceStore,
    workspace_state: EpisodeWorkspace,
    asset_id: str,
    tags: Sequence[str],
    max_tags: int,
) -> tuple[EpisodeWorkspace, str | None]:
    """Publish a curated tag selection to ``copy/selected/<asset_id>.md``.

    Returns the (possibly updated) workspace state and an error message when a tag
    contains a line break or the selection exceeds *max_tags*; nothing is written in
    that case. The cap is checked on the published text as it will be read back, so no
    entry can expand into several tags. A curated selection replaces any
    whole-candidate selection.
    """
    normalized = normalize_tag_values(tags)
    if any("\n" in tag or "\r" in tag for tag in normalized):
        return workspace_state, f"Tags for {asset_id} must not contain line breaks"
    content = render_selected_tags_markdown(asset_id, normalized)
    error = tag_cap_error(asset_id, max(len(normalized), len(parse_tag_list(content))), max_tags)
    if error is not None:
        return workspace_state, error
    store.write_selected_text(asset_id, TextFormat.markdown, content)
    assets_by_id = {asset.asset_id: asset for asset in workspace_state.assets}
    existing = assets_by_id.get(asset_id)
    if existing is None or existing.selected_candidate_id is None:
        return workspace_state, None
    assets_by_id[asset_id] = existing.model_copy(update={"selected_candidate_id": None})
    updated = update_workspace_assets(workspace_state, assets_by_id)
    store.write_state(updated)
    return updated, None


def find_candidate_by_id(candidates: list[Candidate], candidate_id: UUID) -> Candidate | None:
    """Find a candidate by its UUID, or return None."""
    for candidate in candidates:
        if candidate.candidate_id == candidate_id:
            return candidate
    return None


def validate_asset_id(asset_id: str) -> None:
    """Raise ValueError if asset_id contains path separators."""
    if "/" in asset_id or "\\" in asset_id:
        raise ValueError("asset_id must not contain path separators")


def _asset_kind(asset_id: str) -> AssetKind | None:
    try:
        return AssetKind(asset_id)
    except ValueError:
        return None


def custom_selection_format(asset_id: str) -> TextFormat:
    """Return the format a custom selection of ``asset_id`` is stored in."""
    return TextFormat.plain if asset_id in _PLAIN_SELECTION_ASSETS else TextFormat.markdown


def suggest_slug(text: str) -> str:
    """Turn arbitrary text into a lowercase, hyphen-separated, URL-safe slug."""
    lowered = text.lower().translate(_GERMAN_TRANSLITERATIONS)
    ascii_text = unicodedata.normalize("NFKD", lowered).encode("ascii", "ignore").decode("ascii")
    return _NON_SLUG_CHARS_RE.sub("-", ascii_text).strip("-")


def validate_slug(slug: str) -> None:
    """Raise ValueError unless ``slug`` is lowercase, hyphen-separated and URL-safe."""
    if _SLUG_RE.fullmatch(slug):
        return
    message = f"Invalid slug {slug!r}: use lowercase letters a-z, digits and single hyphens between words"
    suggestion = suggest_slug(slug)
    if suggestion:
        message += f" (e.g. {suggestion!r})"
    raise ValueError(message)


def normalize_custom_selection_text(asset_id: str, text: str) -> str:
    """Validate and normalize producer-written text for ``asset_id``; raise ValueError when invalid."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if asset_id in _SINGLE_LINE_ASSETS:
        normalized = normalized.strip()
        if "\n" in normalized:
            raise ValueError(f"{asset_id} must be a single line")
    else:
        normalized = normalized.strip("\n").rstrip()
    if not normalized.strip():
        raise ValueError(f"Custom text for {asset_id} must not be empty")
    if asset_id == AssetKind.slug.value:
        validate_slug(normalized)
    return normalized


def read_current_selection(layout: EpisodeWorkspaceLayout, asset_id: str) -> tuple[TextFormat, str] | None:
    """Return the format and text of the published selection for ``asset_id``, if any."""
    for fmt in (TextFormat.markdown, TextFormat.plain, TextFormat.html):
        path = layout.selected_text_path(asset_id, fmt)
        if path.exists():
            return fmt, path.read_text(encoding="utf-8")
    return None


def clear_manual_selection_provenance(workspace: EpisodeWorkspace, asset_id: str) -> EpisodeWorkspace:
    """Drop the manual-selection marker for ``asset_id`` (e.g. after a candidate was picked)."""
    kept = [ref for ref in workspace.provenance if not _is_manual_selection_ref(ref, asset_id)]
    if len(kept) == len(workspace.provenance):
        return workspace
    return workspace.model_copy(update={"provenance": kept})


def has_manual_selection_provenance(workspace: EpisodeWorkspace, asset_id: str) -> bool:
    return any(_is_manual_selection_ref(ref, asset_id) for ref in workspace.provenance)


def _is_manual_selection_ref(ref: ProvenanceRef, asset_id: str) -> bool:
    return ref.kind == MANUAL_SELECTION_PROVENANCE_KIND and ref.ref == asset_id


def save_custom_selection(
    *,
    store: EpisodeWorkspaceStore,
    workspace: EpisodeWorkspace,
    asset_id: str,
    candidates: list[Candidate],
    text: str,
    based_on: Candidate | None = None,
) -> EpisodeWorkspace:
    """Publish producer-written ``text`` as the selection for ``asset_id`` and record it in state.json.

    The asset keeps its candidates but has no ``selected_candidate_id``; a
    ``manual_selection`` provenance entry on the workspace (plus a JSON record
    under ``copy/provenance/manual_selection/``) notes the manual edit and the
    candidate it started from. Raises ValueError for invalid input, including tag
    assets: they are curated tag by tag (capped at ``tags.max``) via write_selected_tags.
    """
    validate_asset_id(asset_id)
    if asset_id in TAG_ASSET_IDS:
        raise ValueError(f"{asset_id} is curated tag by tag; use the tag editor instead of custom text")
    normalized = normalize_custom_selection_text(asset_id, text)
    fmt = custom_selection_format(asset_id)

    store.write_selected_text(asset_id, fmt, normalized)

    created_at = datetime.now(UTC)
    provenance = ProvenanceRef(kind=MANUAL_SELECTION_PROVENANCE_KIND, ref=asset_id, created_at=created_at)
    store.write_provenance_json(
        provenance,
        {
            "asset_id": asset_id,
            "format": fmt.value,
            "based_on_candidate_id": str(based_on.candidate_id) if based_on is not None else None,
            "edited": based_on is None or based_on.content.strip() != normalized.strip(),
        },
    )

    assets_by_id = {asset.asset_id: asset for asset in workspace.assets}
    assets_by_id[asset_id] = build_asset(
        asset_id=asset_id,
        existing=assets_by_id.get(asset_id),
        candidates=candidates,
        selected_candidate_id=None,
    )
    updated = update_workspace_assets(clear_manual_selection_provenance(workspace, asset_id), assets_by_id)
    updated = updated.model_copy(update={"provenance": [*updated.provenance, provenance]})
    store.write_state(updated)
    return updated
