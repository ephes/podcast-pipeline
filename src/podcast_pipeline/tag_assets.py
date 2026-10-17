"""House-style rules for tag-like assets (``cms_tags``, ``audio_tags``, ``itunes_keywords``).

Generated tag candidates pass through a deterministic post-filter that drops show-level
constants, host names and prose-like entries and caps the count. Optional few-shot
examples from past episodes (``podcast rss-examples`` / ``podcast cms-examples`` JSONL)
show the model the published tag style. For picking, all candidates of a tag asset are
merged into one suggestion pool ordered by how many candidates proposed each tag.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from podcast_pipeline.agent_cli_config import global_config_path
from podcast_pipeline.domain.models import AssetKind
from podcast_pipeline.few_shot_selector import FewShotExampleRecord, load_few_shot_records, select_few_shot_records
from podcast_pipeline.prompting import FewShotExample
from podcast_pipeline.tag_parsing import parse_tag_list

TAG_ASSET_IDS: frozenset[str] = frozenset(
    {AssetKind.cms_tags.value, AssetKind.audio_tags.value, AssetKind.itunes_keywords.value},
)

DEFAULT_MAX_TAGS = 12
DEFAULT_EXAMPLES_LIMIT = 5
MAX_TAG_WORDS = 4
"""Entries with more words than this are prose, not tags, and are dropped."""

DEFAULT_EXCLUDED_TAGS: tuple[str, ...] = (
    "podcast",
    "python podcast",
    "pythonpodcast",
    "deutsch",
    "german",
    "englisch",
    "english",
)
"""Show-level and language terms that apply to every episode and never help discovery."""

_SEPARATOR_RE = re.compile(r"[\s_\-]+")
_MIN_HOST_TOKEN_LENGTH = 3


class TagConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class TagSettings:
    exclude: tuple[str, ...] = DEFAULT_EXCLUDED_TAGS
    max_tags: int = DEFAULT_MAX_TAGS
    examples: tuple[FewShotExampleRecord, ...] = ()
    examples_limit: int = DEFAULT_EXAMPLES_LIMIT


@dataclass(frozen=True)
class TagPoolEntry:
    tag: str
    count: int
    """Number of candidates that proposed this tag."""


def load_tag_settings(*, workspace: Path | None, include_examples: bool = True) -> TagSettings:
    """Resolve the ``tags`` section from the global config and ``episode.yaml``.

    ``exclude`` lists are combined with the built-in defaults; ``max``, ``examples`` and
    ``examples_limit`` from ``episode.yaml`` override the global config. A relative
    ``examples`` path resolves against the directory of the file that sets it. With
    *include_examples* false the examples file is not read.
    """
    global_path = global_config_path()
    sections: list[tuple[Mapping[str, Any], str, Path]] = [
        (_load_tags_section(global_path), str(global_path), global_path.parent),
    ]
    if workspace is not None:
        episode_path = workspace / "episode.yaml"
        sections.append((_load_tags_section(episode_path), str(episode_path), workspace))

    exclude: list[str] = list(DEFAULT_EXCLUDED_TAGS)
    max_tags = DEFAULT_MAX_TAGS
    examples_path: Path | None = None
    examples_limit = DEFAULT_EXAMPLES_LIMIT
    for section, source, base_dir in sections:
        exclude.extend(_parse_exclude(section.get("exclude"), source=source))
        max_tags = _parse_positive_int(section.get("max"), max_tags, source=source, key="tags.max")
        examples_limit = _parse_positive_int(
            section.get("examples_limit"),
            examples_limit,
            source=source,
            key="tags.examples_limit",
        )
        examples_path = _parse_examples_path(section.get("examples"), examples_path, source=source, base=base_dir)

    examples: tuple[FewShotExampleRecord, ...] = ()
    if examples_path is not None and include_examples:
        examples = _load_examples(examples_path)
    return TagSettings(
        exclude=tuple(exclude),
        max_tags=max_tags,
        examples=examples,
        examples_limit=examples_limit,
    )


def load_episode_tag_settings(workspace: Path) -> tuple[TagSettings, list[str]]:
    """Tag settings (without examples) plus ``episode.yaml`` hosts for picking UIs.

    Invalid tag configuration falls back to the built-in defaults so pickers stay usable.
    """
    try:
        settings = load_tag_settings(workspace=workspace, include_examples=False)
    except TagConfigError:
        settings = TagSettings()
    hosts: list[str] = []
    try:
        loaded = yaml.safe_load((workspace / "episode.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        loaded = None
    if isinstance(loaded, Mapping):
        raw_hosts = loaded.get("hosts")
        if isinstance(raw_hosts, list):
            hosts = [host for host in raw_hosts if isinstance(host, str)]
    return settings, hosts


def build_tag_pool(
    contents: Iterable[str],
    *,
    settings: TagSettings,
    hosts: Sequence[str] | None = None,
) -> list[TagPoolEntry]:
    """Merge tag candidates into one filtered pool, most-agreed tags first.

    Each candidate counts at most once per tag (case and separators ignored); ties keep
    first-seen order. The pool itself is not capped.
    """
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for content in contents:
        for tag in filter_tags(_parse_generated(content), settings=settings, hosts=hosts, cap=False):
            key = _tag_key(tag)
            display.setdefault(key, tag)
            counts[key] = counts.get(key, 0) + 1
    order = {key: idx for idx, key in enumerate(display)}
    ranked = sorted(display, key=lambda key: (-counts[key], order[key]))
    return [TagPoolEntry(tag=display[key], count=counts[key]) for key in ranked]


def render_selected_tags_markdown(asset_id: str, tags: Sequence[str]) -> str:
    """Render a picked tag selection for ``copy/selected/<asset_id>.md``."""
    headings = {
        AssetKind.audio_tags.value: "Audio tags",
        AssetKind.cms_tags.value: "CMS tags",
        AssetKind.itunes_keywords.value: "iTunes keywords",
    }
    lines = [f"# {headings.get(asset_id, asset_id)}", ""]
    lines.extend(f"- {tag}" for tag in tags)
    return "\n".join(lines).rstrip() + "\n"


def filter_tags(
    tags: Iterable[str],
    *,
    settings: TagSettings,
    hosts: Sequence[str] | None = None,
    cap: bool = True,
) -> list[str]:
    """Drop excluded, host-name and prose-like tags, then cap the count at ``settings.max_tags``.

    Pass ``cap=False`` to keep every remaining tag.
    """
    excluded = {_tag_key(value) for value in settings.exclude}
    host_keys, host_tokens = _host_keys(hosts)
    kept: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = raw.strip().lstrip("#").strip()
        key = _tag_key(tag)
        if not key or key in seen:
            continue
        tokens = key.split(" ")
        if key in excluded or key in host_keys or host_tokens.intersection(tokens):
            continue
        if len(tokens) > MAX_TAG_WORDS:
            continue
        seen.add(key)
        kept.append(tag)
        if cap and len(kept) >= settings.max_tags:
            break
    return kept


def render_tags(asset_id: str, tags: Sequence[str]) -> str:
    """Render tags in the canonical content format of the given tag asset."""
    if asset_id == AssetKind.itunes_keywords.value:
        return ", ".join(tags)
    return "\n".join(f"- {tag}" for tag in tags)


def postprocess_tag_content(
    asset_id: str,
    content: str,
    *,
    settings: TagSettings,
    hosts: Sequence[str] | None = None,
) -> str:
    """Filter generated tag content; non-tag assets pass through unchanged."""
    if asset_id not in TAG_ASSET_IDS:
        return content
    return render_tags(asset_id, filter_tags(_parse_generated(content), settings=settings, hosts=hosts))


def tag_few_shots(
    asset_id: str,
    *,
    settings: TagSettings,
    topics: Sequence[str],
    hosts: Sequence[str] | None = None,
) -> tuple[FewShotExample, ...]:
    """Select past-episode examples whose published tags show the house style."""
    if asset_id not in TAG_ASSET_IDS or not settings.examples:
        return ()
    usable: list[tuple[FewShotExampleRecord, list[str]]] = []
    for record in settings.examples:
        tags = filter_tags(_record_tags(record, asset_id), settings=settings, hosts=hosts)
        if tags:
            usable.append((record, tags))
    if not usable:
        return ()
    tags_by_id = {record.example_id: tags for record, tags in usable}
    selected = select_few_shot_records(
        examples=[record for record, _ in usable],
        topics=topics,
        limit=settings.examples_limit,
    )
    return tuple(
        FewShotExample(input_text=record.input_text, output_text=render_tags(asset_id, tags_by_id[record.example_id]))
        for record in selected
    )


def _parse_generated(content: str) -> list[str]:
    # Keep tag boundaries: a long separator-free line is prose (dropped by the word
    # limit), not a keyword blob to split into single words.
    return parse_tag_list(content, split_words=False)


def tag_cap_error(asset_id: str, count: int, max_tags: int) -> str | None:
    """Return the error for a tag selection above the configured cap, else ``None``."""
    if count > max_tags:
        return f"At most {max_tags} tags can be selected for {asset_id} ({count} given)"
    return None


def _record_tags(record: FewShotExampleRecord, asset_id: str) -> tuple[str, ...]:
    # CMS exports carry Wagtail tags; RSS exports carry itunes:keywords.
    if asset_id == AssetKind.cms_tags.value:
        return record.tags or record.keywords
    return record.keywords or record.tags


def _tag_key(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip().lstrip("#").casefold())
    return _SEPARATOR_RE.sub(" ", normalized).strip()


def _host_keys(hosts: Sequence[str] | None) -> tuple[set[str], set[str]]:
    keys: set[str] = set()
    tokens: set[str] = set()
    for host in hosts or ():
        key = _tag_key(host)
        if not key:
            continue
        keys.add(key)
        tokens.update(token for token in key.split(" ") if len(token) >= _MIN_HOST_TOKEN_LENGTH)
    return keys, tokens


def _load_tags_section(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise TagConfigError(f"Cannot read UTF-8 configuration at {path}: {type(exc).__name__}") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TagConfigError(f"Invalid YAML at {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise TagConfigError(f"Expected mapping YAML at {path}")
    section = loaded.get("tags")
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise TagConfigError(f"Expected mapping for tags in {path}")
    return section


def _parse_exclude(value: object, *, source: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise TagConfigError(f"tags.exclude must be a list of non-empty strings in {source}")
    return [item.strip() for item in value]


def _parse_positive_int(value: object, fallback: int, *, source: str, key: str) -> int:
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TagConfigError(f"{key} must be a positive integer in {source}")
    return value


def _parse_examples_path(value: object, fallback: Path | None, *, source: str, base: Path) -> Path | None:
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip():
        raise TagConfigError(f"tags.examples must be a non-empty path string in {source}")
    path = Path(value.strip()).expanduser()
    return path if path.is_absolute() else base / path


def _load_examples(path: Path) -> tuple[FewShotExampleRecord, ...]:
    try:
        return tuple(load_few_shot_records(path))
    except FileNotFoundError as exc:
        raise TagConfigError(f"Tag examples file not found (tags.examples): {path}") from exc
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise TagConfigError(f"Cannot load tag examples from {path} (tags.examples): {exc}") from exc
