"""Build the django-cast editor API requests for an episode draft from a workspace.

Everything here is pure: it reads ``episode.yaml``, the global config and the
selected copy, and returns the audio upload and the episode payload. The HTTP
calls live in :mod:`podcast_pipeline.cms_api`.
"""

from __future__ import annotations

import html
import math
import re
import stat
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html.entities import html5
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from podcast_pipeline.agent_cli_config import global_config_path
from podcast_pipeline.domain.models import EpisodeWorkspace, TextFormat
from podcast_pipeline.file_inputs import read_workspace_utf8
from podcast_pipeline.markdown_html import markdown_to_deterministic_html
from podcast_pipeline.pick_core import read_current_selection, validate_slug
from podcast_pipeline.tag_parsing import itunes_keyword_delimiter_error, parse_tag_list
from podcast_pipeline.workspace_store import (
    EpisodeWorkspaceLayout,
    WorkspaceStoreError,
    read_optional_yaml_mapping,
)


class CmsConfigError(ValueError):
    pass


DEFAULT_TOKEN_ENV = "PODCAST_PIPELINE_CMS_TOKEN"
DEFAULT_DETAIL_INTRO_HTML = "<h2>Shownotes</h2>"
# django-cast ``Episode.EXPLICIT_CHOICES``: 1 = yes, 2 = no, 3 = clean.
DEFAULT_EXPLICIT = 2
_EXPLICIT_CHOICES = frozenset({1, 2, 3})
_MAX_CHAPTER_STATE_BYTES = 8 * 1024 * 1024
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
    selected_copy: tuple[tuple[TextFormat, str] | None, ...] = ()
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
    selections = {
        asset: read_current_selection(layout, asset)
        for asset in (
            "title_detail",
            "title_seo",
            "slug",
            "description",
            "shownotes",
            "summary_short",
            "cms_tags",
            "itunes_keywords",
            "subtitle_auphonic",
        )
    }
    selected_text = {asset: selection[1] if selection is not None else None for asset, selection in selections.items()}

    title = _first_line(selected_text.get("title_detail")) or _first_line(selected_text.get("title_seo"))
    if title is None:
        raise CmsConfigError("No selected title: pick title_detail (or title_seo) first.")
    slug = _validate_selected_slug(selected_text["slug"])

    description = selections["description"]
    if description is None:
        raise CmsConfigError("No selected description: pick the description asset first (it becomes the overview).")
    overview_html = link_hosts(
        _description_html(*description),
        hosts=_episode_hosts(episode_yaml),
        host_links=config.host_links or {},
    )
    shownotes = selections["shownotes"]
    detail_html = config.detail_intro_html + (_to_html(*shownotes) if shownotes is not None else "")

    keywords = parse_tag_list(selected_text.get("itunes_keywords"))
    keyword_error = itunes_keyword_delimiter_error(keywords)
    if keyword_error is not None:
        raise CmsConfigError(keyword_error)

    episode_fields: dict[str, Any] = {
        "title": title,
        "slug": slug,
        "seo_title": _first_line(selected_text.get("title_seo")) or "",
        "search_description": _plain_text(selected_text.get("summary_short")),
        "tags": parse_tag_list(selected_text.get("cms_tags")),
        "keywords": ",".join(keywords),
        "explicit": config.explicit,
    }

    audio_file = resolve_audio_file(episode_yaml=episode_yaml, workspace=workspace, config=config)
    subtitle = _first_line(selected_text.get("subtitle_auphonic")) or _first_line(selected_text.get("summary_short"))
    audio = AudioUpload(
        file=audio_file,
        title=title,
        subtitle=subtitle or "",
        chaptermarks=format_chaptermarks(_episode_chapters(episode_yaml, layout)),
        collection=config.audio_collection,
        diarization_mode=config.transcript_diarization,
    )
    extra_audio_files = _extra_audio_files(audio_file)
    if config.audio_file is None:
        for item in extra_audio_files:
            _contained_output(item.path, layout.auphonic_outputs_dir)
    hosts = _episode_hosts(episode_yaml) or []
    known = config.contributors or {}
    return CmsDraftPlan(
        slug=slug,
        title=title,
        episode_fields=episode_fields,
        overview_html=overview_html,
        detail_html=detail_html,
        audio=audio,
        extra_audio_files=extra_audio_files,
        contributors=tuple(known[name] for name in hosts if name in known),
        unmapped_hosts=tuple(name for name in hosts if name not in known),
        selected_copy=tuple(selections.values()),
        hosts_listed=bool(hosts),
    )


def selected_slug(workspace: Path) -> str:
    return _validate_selected_slug(_selected(EpisodeWorkspaceLayout(root=workspace), "slug"))


def _validate_selected_slug(text: str | None) -> str:
    slug = _first_line(text)
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
    _validate_output_basename(basename)
    preferred = outputs / f"{basename}.mp3"
    _contained_output(preferred, outputs)
    try:
        if _is_regular_file(preferred):
            return AudioFile(field="mp3", path=preferred)
        mp3s = sorted(path for path in outputs.iterdir() if path.name.endswith(".mp3") and _is_regular_file(path))
    except (FileNotFoundError, NotADirectoryError):
        mp3s = []
    except OSError as exc:
        raise CmsConfigError(f"Could not inspect MP3 files in {outputs}: {exc}") from exc
    if len(mp3s) == 1:
        _contained_output(mp3s[0], outputs)
        return AudioFile(field="mp3", path=mp3s[0])
    if mp3s:
        names = ", ".join(path.name for path in mp3s)
        raise CmsConfigError(f"Several MP3 files in {outputs} ({names}); set cms.audio_file in episode.yaml.")
    raise CmsConfigError(
        f"No MP3 found in {outputs}: run `podcast produce` first or set cms.audio_file in episode.yaml."
    )


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


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


def _validate_output_basename(basename: str) -> None:
    # Match Auphonic's output component rules, including Windows separators on every platform.
    if "/" in basename or "\\" in basename or basename in {".", ".."} or "\0" in basename:
        raise CmsConfigError("Implicit CMS output basename must be a file name without directories.")


def _contained_output(path: Path, outputs: Path) -> None:
    if not path.resolve().is_relative_to(outputs.resolve()):
        raise CmsConfigError("Implicit CMS audio resolves outside auphonic/outputs; set cms.audio_file explicitly.")


def _output_basename(episode_yaml: Mapping[str, Any]) -> str | None:
    auphonic = episode_yaml.get("auphonic")
    if isinstance(auphonic, Mapping) and auphonic.get("output_basename") is not None:
        value = auphonic["output_basename"]
        if not isinstance(value, str) or not value.strip():
            raise CmsConfigError("auphonic.output_basename must be a non-empty string")
        return value.strip()
    episode_id = episode_yaml.get("episode_id")
    if isinstance(episode_id, str) and episode_id.strip():
        return episode_id.strip()
    return None


def format_chaptermarks(chapters: Sequence[tuple[float, str]]) -> str:
    """django-cast chapter text, rounded to its TimeField's microsecond precision."""
    lines = []
    previous = -1
    for idx, (start, title) in enumerate(chapters, start=1):
        start = _chapter_seconds(start, idx=idx)
        total = round(start * 1_000_000)
        if total >= 86_400_000_000:
            raise CmsConfigError(f"CMS chapter[{idx}].start must be before 24:00:00 at microsecond precision.")
        if total <= previous:
            raise CmsConfigError("CMS chapter starts must increase at microsecond precision.")
        previous = total
        title = " ".join(title.split())
        if not title:
            raise CmsConfigError(f"CMS chapter[{idx}].title must be a non-empty string.")
        whole, micros = divmod(total, 1_000_000)
        hours, rest = divmod(whole, 3600)
        minutes, seconds = divmod(rest, 60)
        fraction = f".{micros:06d}" if micros else ""
        lines.append(f"{hours:02d}:{minutes:02d}:{seconds:02d}{fraction} {title}")
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
    path = layout.state_json
    try:
        try:
            # A dangling link is present. Once observed, disappearance during the read is an error too.
            path.lstat()
        except FileNotFoundError:
            return []
        raw = read_workspace_utf8(layout.root, path.relative_to(layout.root), max_bytes=_MAX_CHAPTER_STATE_BYTES)
    except (OSError, ValueError) as exc:
        raise CmsConfigError(
            f"Cannot read CMS chapters from {path}: expected a regular UTF-8 file within 8 MiB."
        ) from exc
    try:
        workspace = EpisodeWorkspace.from_json(raw)
    except ValueError as exc:
        raise CmsConfigError(f"Cannot read CMS chapters from {path}: invalid workspace state.") from exc
    return [(chapter.start_sec, chapter.title) for chapter in workspace.chapters]


def _chapter_seconds(value: object, *, idx: int) -> float:
    message = f"auphonic.chapters[{idx}].start must be seconds or HH:MM:SS"
    if isinstance(value, bool) or value is None:
        raise CmsConfigError(message)
    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
        except OverflowError as exc:
            raise CmsConfigError(message) from exc
    elif isinstance(value, str):
        seconds = _parse_clock(value.strip(), message=message)
    else:
        raise CmsConfigError(message)
    if not math.isfinite(seconds) or not 0 <= seconds < 86400:
        raise CmsConfigError(message)
    return seconds


def _parse_clock(text: str, *, message: str) -> float:
    if ":" not in text:
        # Plain ASCII seconds only: float() would also accept "1_0", "1e3",
        # signs and non-ASCII digits.
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
            raise CmsConfigError(message)
        return float(text)
    # Only the final seconds component may be fractional. No signs or overflow
    # components: do not silently repair a malformed clock by summing it.
    if not re.fullmatch(r"(?:[0-9]{1,2}:)?[0-9]{1,2}:[0-9]{1,2}(?:\.[0-9]+)?", text):
        raise CmsConfigError(message)
    numbers = [float(part) for part in text.split(":")]
    if any(number >= 60 for number in numbers[-2:]):
        raise CmsConfigError(message)
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def _episode_hosts(episode_yaml: Mapping[str, Any]) -> list[str] | None:
    hosts = episode_yaml.get("hosts")
    if not isinstance(hosts, list):
        return None
    return [host.strip() for host in hosts if isinstance(host, str) and host.strip()]


class _HostTextParser(HTMLParser):
    """Record eligible source spans without reserializing or decoding markup."""

    _HIDDEN = frozenset({"script", "style", "template", "head", "title", "textarea"})

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_offsets = [0, *(match.end() for match in re.finditer("\n", source))]
        self.spans: list[tuple[int, int]] = []
        self.hrefs: set[str] = set()
        self.ambiguous_self_closing = False
        self.depths = dict.fromkeys(self._HIDDEN | {"a"}, 0)

    def source_offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record_href(tag, attrs)
        if tag in self.depths:
            self.depths[tag] += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # HTML browsers ignore the slash on these nonvoid tags; HTMLParser does
        # not. Keep ambiguous source unchanged instead of trusting text spans.
        if tag in self.depths:
            self.ambiguous_self_closing = True
        self._record_href(tag, attrs)

    def _record_href(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.hrefs.update(value for key, value in attrs if key == "href" and value is not None)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.depths:
            self.depths[tag] = max(0, self.depths[tag] - 1)

    def handle_data(self, data: str) -> None:
        if any(self.depths.values()):
            return
        start = self.source_offset()
        end = start + len(data)
        if self.spans and self.spans[-1][1] == start:
            self.spans[-1] = (self.spans[-1][0], end)
        else:
            self.spans.append((start, end))

    def handle_entityref(self, name: str) -> None:
        self._entity_text("&" + name)

    def handle_charref(self, name: str) -> None:
        self._entity_text("&#" + name)

    def _entity_text(self, token: str) -> None:
        if self.source.startswith(token + ";", self.source_offset()):
            token += ";"
        self.handle_data(token)


def link_hosts(html_text: str, *, hosts: Sequence[str] | None, host_links: Mapping[str, str]) -> str:
    """Link the first eligible text mention, preserving source markup exactly.

    Existing anchors, comments and script/style/template text are excluded. A host
    whose URL already occurs in an anchor is left alone. Incomplete HTML is kept
    unchanged; feeding one complete buffer avoids repeated incomplete-token scans.
    """
    names = list(host_links) if hosts is None else [name for name in hosts if name in host_links]
    for name in names:
        html_text = _link_first_mention(html_text, name=name, url=host_links[name])
    return html_text


def _link_first_mention(html_text: str, *, name: str, url: str) -> str:
    parser = _HostTextParser(html_text)
    try:
        parser.feed(html_text)
    except AssertionError:  # malformed declarations on supported Python versions
        return html_text
    # Do not close(): HTMLParser would reinterpret an unfinished token as text.
    if parser.ambiguous_self_closing or parser.source_offset() != len(html_text) or url in parser.hrefs:
        return html_text
    pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
    for start, end in parser.spans:
        linked = _link_in_text(html_text, start, end, pattern=pattern, url=url)
        if linked is not None:
            return linked
    return html_text


def _link_in_text(html_text: str, start: int, end: int, *, pattern: re.Pattern[str], url: str) -> str | None:
    parts: list[str] = []
    references: list[tuple[int, int, int]] = []  # decoded start, decoded end, source end
    source_end = start
    decoded_end = 0
    for reference in _CHARACTER_REFERENCE_RE.finditer(html_text, start, end):
        decoded = _decode_reference(reference)
        if decoded is None:
            continue
        replacement, length = decoded
        parts.append(html_text[source_end : reference.start()])
        parts.append(replacement)
        decoded_start = decoded_end + reference.start() - source_end
        decoded_end = decoded_start + len(replacement)
        source_end = reference.start() + length
        references.append((decoded_start, decoded_end, source_end))
    parts.append(html_text[source_end:end])

    decoded_text = "".join(parts)
    search_from = 0
    while match := pattern.search(decoded_text, search_from):
        left = _source_text_boundary(references, start, match.start())
        right = _source_text_boundary(references, start, match.end())
        if left is not None and right is not None:
            anchor = f'<a href="{html.escape(url)}">{html_text[left:right]}</a>'
            return html_text[:left] + anchor + html_text[right:]
        search_from = match.start() + 1
    return None


_CHARACTER_REFERENCE_RE = re.compile(
    r"&(?:#(?P<number>[0-9]+|[xX][0-9a-fA-F]+);?|(?P<name>[a-zA-Z][a-zA-Z0-9]{0,30};?))"
)


def _decode_reference(reference: re.Match[str]) -> tuple[str, int] | None:
    if number := reference["number"]:
        hexadecimal = number[0] in "xX"
        digits = (number[1:] if hexadecimal else number).lstrip("0")
        # Clamp out-of-range inputs before integer conversion, including arbitrarily long references.
        codepoint = int(digits or "0", 16 if hexadecimal else 10) if len(digits) <= 7 else 0x110000
        return html.unescape(f"&#{codepoint};") or chr(codepoint), reference.end() - reference.start()

    # Legacy named references can omit the semicolon and end before the scanned name does.
    name = reference["name"]
    for length in range(len(name), 0, -1):
        if replacement := html5.get(name[:length]):
            return replacement, length + 1  # including '&'
    return None


def _source_text_boundary(references: list[tuple[int, int, int]], start: int, offset: int) -> int | None:
    index = bisect_right(references, offset, key=lambda ref: ref[1])
    if index < len(references) and references[index][0] < offset:
        return None  # Inserting markup here would split a reference that expands to multiple characters.
    if index == 0:
        return start + offset
    _, decoded_end, source_end = references[index - 1]
    return source_end + offset - decoded_end


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
    # The global config is user-owned, not workspace content, and other readers follow a
    # dotfiles symlink: resolve it, then read the target as one bounded regular file.
    try:
        return read_optional_yaml_mapping(path.resolve(), max_bytes=1024 * 1024)
    except WorkspaceStoreError as exc:
        raise CmsConfigError(str(exc)) from exc


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
        if not isinstance(role, str) or role not in CONTRIBUTOR_ROLES:
            raise CmsConfigError(f"cms.contributors.{name}.role in {source} must be host or guest")
        refs[name.strip()] = ContributorRef(slug=slug.strip(), role=role)
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
