"""Build the django-cast editor API requests for an episode draft from a workspace.

Everything here is pure: it reads ``episode.yaml``, the global config and the
selected copy, and returns the audio upload and the episode payload. The HTTP
calls live in :mod:`podcast_pipeline.cms_api`.
"""

from __future__ import annotations

import html
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from podcast_pipeline.agent_cli_config import global_config_path
from podcast_pipeline.domain.models import TextFormat
from podcast_pipeline.markdown_html import markdown_to_deterministic_html
from podcast_pipeline.pick_core import read_current_selection, validate_slug
from podcast_pipeline.tag_parsing import parse_tag_list
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout, EpisodeWorkspaceStore


class CmsConfigError(ValueError):
    pass


DEFAULT_TOKEN_ENV = "PODCAST_PIPELINE_CMS_TOKEN"
DEFAULT_DETAIL_INTRO_HTML = "<h2>Shownotes</h2>"
# django-cast ``Episode.EXPLICIT_CHOICES``: 1 = yes, 2 = no, 3 = clean.
DEFAULT_EXPLICIT = 2
_EXPLICIT_CHOICES = frozenset({1, 2, 3})
# django-cast ``Audio.TranscriptDiarizationMode``. Speaker labels need "enabled": "inherit" follows the site's
# Voxhelm settings, which may switch diarization off.
DEFAULT_TRANSCRIPT_DIARIZATION = "enabled"
DIARIZATION_MODES = frozenset({"inherit", "enabled", "disabled"})
# django-cast ``EpisodeContributor`` roles.
CONTRIBUTOR_ROLES = frozenset({"host", "guest"})

# Form field names of the django-cast audio upload, by output file suffix.
AUDIO_FIELD_BY_SUFFIX: dict[str, str] = {
    ".mp3": "mp3",
    ".m4a": "m4a",
    ".ogg": "oga",
    ".oga": "oga",
    ".opus": "opus",
}
_AUDIO_FIELD_ORDER = ("mp3", "m4a", "oga", "opus")

AUDIO_ID_PLACEHOLDER = "<audio id>"
"""Stands in for the Audio id in ``--dry-run`` output, before anything is uploaded."""


@dataclass(frozen=True)
class CmsConfig:
    base_url: str
    token_env: str = DEFAULT_TOKEN_ENV
    parent_id: int | None = None
    parent_title: str | None = None
    audio_collection: int | None = None
    explicit: int = DEFAULT_EXPLICIT
    detail_intro_html: str = DEFAULT_DETAIL_INTRO_HTML
    host_links: Mapping[str, str] | None = None
    audio_file: str | None = None
    transcript_diarization: str = DEFAULT_TRANSCRIPT_DIARIZATION
    contributors: Mapping[str, ContributorRef] | None = None


@dataclass(frozen=True)
class ContributorRef:
    """A django-cast Contributor snippet (by slug) and the role it has in an episode."""

    slug: str
    role: str = "host"


@dataclass(frozen=True)
class AudioFile:
    field: str
    path: Path


@dataclass(frozen=True)
class AudioUpload:
    """One ``POST api/editor/media/audios/`` request: a single file plus its metadata."""

    file: AudioFile
    title: str
    subtitle: str
    chaptermarks: str
    collection: int | None = None
    diarization_mode: str | None = None

    def form_fields(self) -> dict[str, str]:
        fields = {"title": self.title, "subtitle": self.subtitle, "chaptermarks": self.chaptermarks}
        if self.collection is not None:
            fields["collection"] = str(self.collection)
        if self.diarization_mode is not None:
            fields["transcript_diarization_mode"] = self.diarization_mode
        return fields


@dataclass(frozen=True)
class CmsDraftPlan:
    slug: str
    title: str
    episode_fields: Mapping[str, Any]
    overview_html: str
    detail_html: str
    audio: AudioUpload
    extra_audio_files: tuple[AudioFile, ...]
    contributors: tuple[ContributorRef, ...] = ()
    unmapped_hosts: tuple[str, ...] = ()
    hosts_listed: bool = True
    """Whether episode.yaml names the episode's speakers (``hosts``); without them contributors cannot be checked."""

    def contributors_payload(self) -> list[dict[str, str]]:
        """The episode's ``contributors`` field for a django-cast editor API that accepts it."""
        return [{"slug": item.slug, "role": item.role} for item in self.contributors]

    def episode_payload(self, audio_id: int | str) -> dict[str, Any]:
        """The episode create/update body, pointing ``podcast_audio`` and the overview player at ``audio_id``."""
        payload = dict(self.episode_fields)
        payload["podcast_audio"] = {"id": audio_id}
        # Without the audio block in the overview no player renders on the page.
        payload["overview"] = [
            {"type": "paragraph", "value": self.overview_html},
            {"type": "audio", "value": {"id": audio_id}},
        ]
        payload["detail"] = [{"type": "paragraph", "value": self.detail_html}]
        return payload


def load_cms_config(episode_yaml: Mapping[str, Any]) -> CmsConfig:
    """Merge the ``cms`` section of the global config with the one in ``episode.yaml`` (episode wins)."""
    global_path = global_config_path()
    global_section = _cms_section(_load_yaml_mapping(global_path), source=str(global_path))
    episode_section = _cms_section(episode_yaml, source="episode.yaml")
    merged: dict[str, Any] = {**global_section, **episode_section}
    host_links = {
        **_host_links(global_section.get("host_links"), source=str(global_path)),
        **_host_links(episode_section.get("host_links"), source="episode.yaml"),
    }
    contributors = {
        **_contributors(global_section.get("contributors"), source=str(global_path)),
        **_contributors(episode_section.get("contributors"), source="episode.yaml"),
    }
    diarization = _optional_str(merged.get("transcript_diarization"), key="cms.transcript_diarization")
    if diarization is not None and diarization not in DIARIZATION_MODES:
        raise CmsConfigError("cms.transcript_diarization must be enabled, disabled or inherit")

    base_url = _optional_str(merged.get("base_url"), key="cms.base_url")
    if base_url is None:
        raise CmsConfigError(
            f"Missing cms.base_url (for example https://python-podcast.de/) in {global_path} or episode.yaml."
        )
    validate_base_url(base_url)
    explicit = _optional_int(merged.get("explicit"), key="cms.explicit")
    if explicit is not None and explicit not in _EXPLICIT_CHOICES:
        raise CmsConfigError("cms.explicit must be 1 (yes), 2 (no) or 3 (clean)")
    detail_intro = merged.get("detail_intro_html", DEFAULT_DETAIL_INTRO_HTML)
    if not isinstance(detail_intro, str):
        raise CmsConfigError("cms.detail_intro_html must be a string")
    return CmsConfig(
        base_url=base_url,
        token_env=_optional_str(merged.get("token_env"), key="cms.token_env") or DEFAULT_TOKEN_ENV,
        parent_id=_optional_int(merged.get("parent_id"), key="cms.parent_id"),
        parent_title=_optional_str(merged.get("parent_title"), key="cms.parent_title"),
        audio_collection=_optional_int(merged.get("audio_collection"), key="cms.audio_collection"),
        explicit=DEFAULT_EXPLICIT if explicit is None else explicit,
        detail_intro_html=detail_intro,
        host_links=host_links,
        audio_file=_optional_str(episode_section.get("audio_file"), key="cms.audio_file"),
        transcript_diarization=diarization or DEFAULT_TRANSCRIPT_DIARIZATION,
        contributors=contributors,
    )


def validate_base_url(base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise CmsConfigError(f"cms.base_url must be an https:// URL (got {base_url!r}); the API token is never sent")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise CmsConfigError("cms.base_url must not contain credentials, a query or a fragment")


def build_cms_draft_plan(
    *,
    episode_yaml: Mapping[str, Any],
    workspace: Path,
    config: CmsConfig,
) -> CmsDraftPlan:
    layout = EpisodeWorkspaceLayout(root=workspace)

    title = _first_line(_selected(layout, "title_detail")) or _first_line(_selected(layout, "title_seo"))
    if title is None:
        raise CmsConfigError("No selected title: pick title_detail (or title_seo) first.")
    slug = selected_slug(workspace)

    description = read_current_selection(layout, "description")
    if description is None:
        raise CmsConfigError("No selected description: pick the description asset first (it becomes the overview).")
    overview_html = link_hosts(
        _description_html(*description),
        hosts=_episode_hosts(episode_yaml),
        host_links=config.host_links or {},
    )
    shownotes = read_current_selection(layout, "shownotes")
    detail_html = config.detail_intro_html + (_to_html(*shownotes) if shownotes is not None else "")

    episode_fields: dict[str, Any] = {
        "title": title,
        "slug": slug,
        "seo_title": _first_line(_selected(layout, "title_seo")) or "",
        "search_description": _plain_text(_selected(layout, "summary_short")),
        "tags": parse_tag_list(_selected(layout, "cms_tags")),
        "keywords": ",".join(parse_tag_list(_selected(layout, "itunes_keywords"))),
        "explicit": config.explicit,
    }

    audio_file = resolve_audio_file(episode_yaml=episode_yaml, workspace=workspace, config=config)
    subtitle = _first_line(_selected(layout, "subtitle_auphonic")) or _first_line(_selected(layout, "summary_short"))
    audio = AudioUpload(
        file=audio_file,
        title=title,
        subtitle=subtitle or "",
        chaptermarks=format_chaptermarks(_episode_chapters(episode_yaml, layout)),
        collection=config.audio_collection,
        diarization_mode=config.transcript_diarization,
    )
    hosts = _episode_hosts(episode_yaml) or []
    known = config.contributors or {}
    return CmsDraftPlan(
        slug=slug,
        title=title,
        episode_fields=episode_fields,
        overview_html=overview_html,
        detail_html=detail_html,
        audio=audio,
        extra_audio_files=_extra_audio_files(audio_file),
        contributors=tuple(known[name] for name in hosts if name in known),
        unmapped_hosts=tuple(name for name in hosts if name not in known),
        hosts_listed=bool(hosts),
    )


def selected_slug(workspace: Path) -> str:
    slug = _first_line(_selected(EpisodeWorkspaceLayout(root=workspace), "slug"))
    if slug is None:
        raise CmsConfigError("No selected slug: pick the slug asset first.")
    try:
        validate_slug(slug)
    except ValueError as exc:
        raise CmsConfigError(f"Selected slug: {exc}") from exc
    return slug


def resolve_audio_file(*, episode_yaml: Mapping[str, Any], workspace: Path, config: CmsConfig) -> AudioFile:
    """The file uploaded with the Audio: ``cms.audio_file``, else the Auphonic MP3 output."""
    if config.audio_file is not None:
        path = Path(config.audio_file).expanduser()
        if not path.is_absolute():
            path = workspace / path
        return _audio_file(path, source="cms.audio_file")

    outputs = EpisodeWorkspaceLayout(root=workspace).auphonic_outputs_dir
    basename = _output_basename(episode_yaml) or workspace.name
    preferred = outputs / f"{basename}.mp3"
    if preferred.is_file():
        return AudioFile(field="mp3", path=preferred)
    mp3s = sorted(outputs.glob("*.mp3")) if outputs.is_dir() else []
    if len(mp3s) == 1:
        return AudioFile(field="mp3", path=mp3s[0])
    if mp3s:
        names = ", ".join(path.name for path in mp3s)
        raise CmsConfigError(f"Several MP3 files in {outputs} ({names}); set cms.audio_file in episode.yaml.")
    raise CmsConfigError(
        f"No MP3 found in {outputs}: run `podcast produce` first or set cms.audio_file in episode.yaml."
    )


def _audio_file(path: Path, *, source: str) -> AudioFile:
    field = AUDIO_FIELD_BY_SUFFIX.get(path.suffix.lower())
    if field is None:
        suffixes = ", ".join(sorted(AUDIO_FIELD_BY_SUFFIX))
        raise CmsConfigError(f"{source} {path} must end in one of {suffixes}")
    if not path.is_file():
        raise CmsConfigError(f"{source} {path} does not exist")
    return AudioFile(field=field, path=path)


def _extra_audio_files(primary: AudioFile) -> tuple[AudioFile, ...]:
    """Other formats of the same output (same stem, same directory), one per django-cast audio field."""
    found: dict[str, Path] = {}
    for suffix, field in AUDIO_FIELD_BY_SUFFIX.items():
        if field == primary.field or field in found:
            continue
        candidate = primary.path.with_suffix(suffix)
        if candidate.is_file():
            found[field] = candidate
    return tuple(AudioFile(field=field, path=found[field]) for field in _AUDIO_FIELD_ORDER if field in found)


def _output_basename(episode_yaml: Mapping[str, Any]) -> str | None:
    auphonic = episode_yaml.get("auphonic")
    if isinstance(auphonic, Mapping):
        value = auphonic.get("output_basename")
        if isinstance(value, str) and value.strip():
            return value.strip()
    episode_id = episode_yaml.get("episode_id")
    if isinstance(episode_id, str) and episode_id.strip():
        return episode_id.strip()
    return None


def format_chaptermarks(chapters: Sequence[tuple[float, str]]) -> str:
    """django-cast's ``chaptermarks`` field: one ``HH:MM:SS Title`` line per chapter."""
    lines = []
    for start, title in chapters:
        total = int(start)
        hours, rest = divmod(total, 3600)
        minutes, seconds = divmod(rest, 60)
        lines.append(f"{hours:02d}:{minutes:02d}:{seconds:02d} {' '.join(title.split())}")
    return "\n".join(lines)


def _episode_chapters(episode_yaml: Mapping[str, Any], layout: EpisodeWorkspaceLayout) -> list[tuple[float, str]]:
    auphonic = episode_yaml.get("auphonic")
    raw = auphonic.get("chapters") if isinstance(auphonic, Mapping) else None
    if raw is None:
        return _state_chapters(layout)
    if not isinstance(raw, list):
        raise CmsConfigError("auphonic.chapters must be a list")
    chapters: list[tuple[float, str]] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise CmsConfigError(f"auphonic.chapters[{idx}] needs a start and a title for the CMS chapter marks")
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise CmsConfigError(f"auphonic.chapters[{idx}].title must be a non-empty string")
        start = item.get("start", item.get("start_sec"))
        chapters.append((_chapter_seconds(start, idx=idx), title.strip()))
    return chapters


def _state_chapters(layout: EpisodeWorkspaceLayout) -> list[tuple[float, str]]:
    if not layout.state_json.exists():
        return []
    workspace = EpisodeWorkspaceStore(layout.root).read_state()
    return [(chapter.start_sec, chapter.title) for chapter in workspace.chapters]


def _chapter_seconds(value: object, *, idx: int) -> float:
    message = f"auphonic.chapters[{idx}].start must be seconds or HH:MM:SS"
    if isinstance(value, bool) or value is None:
        raise CmsConfigError(message)
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        seconds = _parse_clock(value.strip(), message=message)
    else:
        raise CmsConfigError(message)
    if not math.isfinite(seconds) or seconds < 0:
        raise CmsConfigError(message)
    return seconds


def _parse_clock(text: str, *, message: str) -> float:
    parts = text.split(":")
    if not 1 <= len(parts) <= 3:
        raise CmsConfigError(message)
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise CmsConfigError(message) from exc
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def _episode_hosts(episode_yaml: Mapping[str, Any]) -> list[str] | None:
    hosts = episode_yaml.get("hosts")
    if not isinstance(hosts, list):
        return None
    return [host.strip() for host in hosts if isinstance(host, str) and host.strip()]


_TAG_RE = re.compile(r"<[^>]*>")


def link_hosts(html_text: str, *, hosts: Sequence[str] | None, host_links: Mapping[str, str]) -> str:
    """Link the first unlinked mention of each host to their profile from ``cms.host_links``.

    Only hosts of this episode (``hosts`` in ``episode.yaml``; all configured hosts when it is
    unset) are linked. A host whose URL already appears in the HTML, or whose name only occurs
    inside existing links, is left alone.
    """
    names = list(host_links) if hosts is None else [name for name in hosts if name in host_links]
    for name in names:
        url = host_links[name]
        if f'href="{html.escape(url)}"' in html_text or f'href="{url}"' in html_text:
            continue
        html_text = _link_first_mention(html_text, name=name, url=url)
    return html_text


def _link_first_mention(html_text: str, *, name: str, url: str) -> str:
    pattern = re.compile(rf"(?<!\w){re.escape(html.escape(name, quote=False))}(?!\w)")
    anchor_depth = 0
    pos = 0
    for tag in _TAG_RE.finditer(html_text):
        if anchor_depth == 0:
            linked = _link_in_text(html_text, pos, tag.start(), pattern=pattern, url=url)
            if linked is not None:
                return linked
        tag_text = tag.group(0).lower()
        if re.match(r"<a[\s>]", tag_text):
            anchor_depth += 1
        elif tag_text.startswith("</a"):
            anchor_depth = max(0, anchor_depth - 1)
        pos = tag.end()
    if anchor_depth == 0:
        linked = _link_in_text(html_text, pos, len(html_text), pattern=pattern, url=url)
        if linked is not None:
            return linked
    return html_text


def _link_in_text(html_text: str, start: int, end: int, *, pattern: re.Pattern[str], url: str) -> str | None:
    match = pattern.search(html_text, start, end)
    if match is None:
        return None
    anchor = f'<a href="{html.escape(url)}">{match.group(0)}</a>'
    return html_text[: match.start()] + anchor + html_text[match.end() :]


_LEADING_HEADING_RE = re.compile(r"\A\s*<h[1-6][^>]*>.*?</h[1-6]>\s*", re.DOTALL | re.IGNORECASE)


def _description_html(fmt: TextFormat, text: str) -> str:
    """The description as overview HTML, without its leading title heading."""
    if fmt == TextFormat.html:
        return _LEADING_HEADING_RE.sub("", text, count=1).strip()
    return _to_html(fmt, _strip_leading_markdown_heading(text))


def _strip_leading_markdown_heading(text: str) -> str:
    return re.sub(r"\A\s*#{1,6} [^\n]*\n*", "", text, count=1)


def _to_html(fmt: TextFormat, text: str) -> str:
    if fmt == TextFormat.html:
        return text.strip()
    return markdown_to_deterministic_html(text.strip())


def _selected(layout: EpisodeWorkspaceLayout, asset_id: str) -> str | None:
    selection = read_current_selection(layout, asset_id)
    return None if selection is None else selection[1]


def _first_line(text: str | None) -> str | None:
    if text is None:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return None


def _plain_text(text: str | None) -> str:
    if text is None:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    return " ".join(lines)


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CmsConfigError(f"Could not read {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise CmsConfigError(f"{path} must contain a mapping")
    return data


def _cms_section(data: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    section = data.get("cms")
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise CmsConfigError(f"cms in {source} must be a mapping")
    return dict(section)


def _host_links(value: object, *, source: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CmsConfigError(f"cms.host_links in {source} must map host names to URLs")
    links: dict[str, str] = {}
    for name, url in value.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(url, str) or not url.strip():
            raise CmsConfigError(f"cms.host_links in {source} must map host names to URLs")
        if urlparse(url.strip()).scheme not in {"http", "https"}:
            raise CmsConfigError(f"cms.host_links.{name} in {source} must be an http(s) URL")
        links[name.strip()] = url.strip()
    return links


def _contributors(value: object, *, source: str) -> dict[str, ContributorRef]:
    """``cms.contributors``: host name -> contributor slug, or -> {slug, role}."""
    if value is None:
        return {}
    message = f"cms.contributors in {source} must map host names to a contributor slug or to {{slug, role}}"
    if not isinstance(value, Mapping):
        raise CmsConfigError(message)
    refs: dict[str, ContributorRef] = {}
    for name, entry in value.items():
        if not isinstance(name, str) or not name.strip():
            raise CmsConfigError(message)
        slug: object = entry
        role: object = "host"
        if isinstance(entry, Mapping):
            slug = entry.get("slug")
            role = entry.get("role", "host")
        if not isinstance(slug, str) or not slug.strip():
            raise CmsConfigError(message)
        if role not in CONTRIBUTOR_ROLES:
            raise CmsConfigError(f"cms.contributors.{name}.role in {source} must be host or guest")
        refs[name.strip()] = ContributorRef(slug=slug.strip(), role=str(role))
    return refs


def _optional_str(value: object, *, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CmsConfigError(f"{key} must be a string")
    return value.strip() or None


def _optional_int(value: object, *, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CmsConfigError(f"{key} must be a positive integer")
    return value
