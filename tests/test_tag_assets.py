from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from podcast_pipeline.asset_candidates_llm import generate_draft_candidates_llm, generate_single_asset_candidates_llm
from podcast_pipeline.domain.intermediate_formats import EpisodeSummary
from podcast_pipeline.domain.models import Asset, AssetKind, Candidate, EpisodeWorkspace, TextFormat
from podcast_pipeline.few_shot_selector import FewShotExampleRecord
from podcast_pipeline.pick_core import write_selected_tags
from podcast_pipeline.prompting import PromptRenderer, default_prompt_registry
from podcast_pipeline.tag_assets import (
    DEFAULT_EXCLUDED_TAGS,
    DEFAULT_MAX_TAGS,
    TagConfigError,
    TagPoolEntry,
    TagSettings,
    build_tag_pool,
    filter_tags,
    load_episode_tag_settings,
    load_tag_settings,
    postprocess_tag_content,
    render_selected_tags_markdown,
    tag_few_shots,
)
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore

_HOSTS = ["Jochen", "Dominik Geldmacher"]


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config" / "config.yaml"
    config.parent.mkdir()
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(config))
    return config


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_filter_tags_drops_show_level_language_and_host_tags() -> None:
    tags = [
        "Python Podcast",
        "python-podcast",
        "Deutsch",
        "#Podcast",
        "Jochen",
        "Dominik",
        "Dominik Geldmacher",
        "python3.15",
        "PEP 649",
        "free-threading",
    ]
    assert filter_tags(tags, settings=TagSettings(), hosts=_HOSTS) == ["python3.15", "PEP 649", "free-threading"]


def test_filter_tags_applies_configured_exclude_case_and_separator_insensitive() -> None:
    settings = TagSettings(exclude=("Softwareentwicklung", "what's new"))
    assert filter_tags(["software-entwicklung", "Softwareentwicklung", "What's  New", "jit"], settings=settings) == [
        "software-entwicklung",
        "jit",
    ]


def test_filter_tags_drops_prose_dedupes_and_caps() -> None:
    settings = TagSettings(max_tags=3)
    tags = ["Ein langer Satz über neue Features", "pypy", "PyPy", "#jit", "repl", "debugging"]
    assert filter_tags(tags, settings=settings) == ["pypy", "jit", "repl"]


def test_filter_tags_default_cap() -> None:
    tags = [f"tag{idx}" for idx in range(30)]
    assert len(filter_tags(tags, settings=TagSettings())) == DEFAULT_MAX_TAGS


def test_postprocess_tag_content_renders_per_asset_format() -> None:
    settings = TagSettings()
    content = "- Python Podcast\n- pypy\n- jit\n- Jochen"
    assert postprocess_tag_content("cms_tags", content, settings=settings, hosts=_HOSTS) == "- pypy\n- jit"
    assert postprocess_tag_content("audio_tags", content, settings=settings, hosts=_HOSTS) == "- pypy\n- jit"
    keywords = postprocess_tag_content("itunes_keywords", "python, Podcast, pypy, jit", settings=settings)
    assert keywords == "python, pypy, jit"
    assert postprocess_tag_content("description", "Python Podcast", settings=settings) == "Python Podcast"


def test_load_tag_settings_defaults_without_config(tmp_path: Path) -> None:
    assert load_tag_settings(workspace=tmp_path) == TagSettings()


def test_load_tag_settings_merges_global_and_episode(tmp_path: Path, isolated_config: Path) -> None:
    _write_jsonl(
        isolated_config.parent / "rss_examples.jsonl",
        [{"input": "Title: A", "output": "<p>A</p>", "keywords": ["python", "jit"]}],
    )
    isolated_config.write_text(
        "tags:\n  exclude: [Programmierung]\n  max: 8\n  examples: rss_examples.jsonl\n  examples_limit: 2\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "episode"
    store = EpisodeWorkspaceStore(workspace)
    store.write_episode_yaml({"episode_id": "ep", "tags": {"exclude": ["Johannes"], "max": 10}})

    settings = load_tag_settings(workspace=workspace)

    assert "Programmierung" in settings.exclude
    assert "Johannes" in settings.exclude
    assert "python podcast" in settings.exclude
    assert settings.max_tags == 10
    assert settings.examples_limit == 2
    assert [record.keywords for record in settings.examples] == [("python", "jit")]


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("tags: [a]\n", "Expected mapping for tags"),
        ("tags:\n  exclude: python\n", "tags.exclude"),
        ("tags:\n  max: 0\n", "tags.max"),
        ("tags:\n  max: true\n", "tags.max"),
        ("tags:\n  examples_limit: -1\n", "tags.examples_limit"),
        ("tags:\n  examples: missing.jsonl\n", "not found"),
        ("tags: {\n", "Invalid YAML"),
    ],
)
def test_load_tag_settings_rejects_invalid_config(isolated_config: Path, yaml_text: str, message: str) -> None:
    isolated_config.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(TagConfigError, match=message):
        load_tag_settings(workspace=None)


def test_load_tag_settings_rejects_invalid_jsonl(isolated_config: Path) -> None:
    (isolated_config.parent / "bad.jsonl").write_text("{not json\n", encoding="utf-8")
    isolated_config.write_text("tags:\n  examples: bad.jsonl\n", encoding="utf-8")
    with pytest.raises(TagConfigError, match="Cannot load tag examples"):
        load_tag_settings(workspace=None)


def _records() -> tuple[FewShotExampleRecord, ...]:
    return (
        FewShotExampleRecord(
            example_id="rss_1",
            input_text="Title: Data Science",
            output_text="<p>...</p>",
            keywords=("data science", "pandas", "polars", "Python Podcast"),
        ),
        FewShotExampleRecord(
            example_id="rss_2",
            input_text="Title: Python 3.13",
            output_text="<p>...</p>",
            keywords=("python", "python3.13", "jit", "nogil"),
        ),
        FewShotExampleRecord(example_id="rss_3", input_text="Title: No tags", output_text="<p>...</p>"),
        FewShotExampleRecord(
            example_id="cms_1",
            input_text="Title: CMS",
            output_text="<p>...</p>",
            tags=("wagtail", "django"),
        ),
    )


def test_tag_few_shots_selects_by_topic_and_renders_filtered_tags() -> None:
    settings = TagSettings(examples=_records(), examples_limit=1)
    shots = tag_few_shots("itunes_keywords", settings=settings, topics=["JIT Compiler"])
    assert [(shot.input_text, shot.output_text) for shot in shots] == [
        ("Title: Python 3.13", "python, python3.13, jit, nogil"),
    ]

    shots = tag_few_shots("audio_tags", settings=TagSettings(examples=_records()), topics=[])
    assert [shot.input_text for shot in shots] == ["Title: Data Science", "Title: Python 3.13", "Title: CMS"]
    assert shots[0].output_text == "- data science\n- pandas\n- polars"


def test_tag_few_shots_skips_non_tag_assets_and_missing_examples() -> None:
    assert tag_few_shots("description", settings=TagSettings(examples=_records()), topics=[]) == ()
    assert tag_few_shots("cms_tags", settings=TagSettings(), topics=[]) == ()


class _ScriptedRunner:
    def __init__(self, content_by_asset: dict[str, str]) -> None:
        self._content = content_by_asset
        self.prompts: dict[str, str] = {}

    def run(self, prompt_text: str) -> dict[str, Any]:
        asset_id = next(kind.value for kind in AssetKind if f"Asset type: {kind.value}\n" in prompt_text)
        self.prompts[asset_id] = prompt_text
        return {"candidates": [{"content": self._content.get(asset_id, "text")}]}


def test_generate_draft_candidates_filters_tags_and_injects_rules_and_examples() -> None:
    noisy = "\n".join(f"- {tag}" for tag in ["Python Podcast", "Jochen", "Deutsch", "pypy", "jit", "Neue Features"])
    runner = _ScriptedRunner({"cms_tags": noisy, "itunes_keywords": "Podcast, pypy, jit, Dominik"})
    settings = TagSettings(exclude=(*DEFAULT_EXCLUDED_TAGS, "Neue Features"), max_tags=8, examples=_records())

    assets = generate_draft_candidates_llm(
        episode_summary=EpisodeSummary(summary_markdown="Summary", key_points=[], topics=["JIT"]),
        chapters=[],
        candidates_per_asset=1,
        runner=runner,
        renderer=PromptRenderer(default_prompt_registry()),
        hosts=_HOSTS,
        tag_settings=settings,
    )

    assert assets["cms_tags"][0].content == "- pypy\n- jit"
    assert assets["itunes_keywords"][0].content == "pypy, jit"
    tag_prompt = runner.prompts["audio_tags"]
    assert "Tag rules:" in tag_prompt
    assert "never more than 8" in tag_prompt
    assert "Few-shot examples:" in tag_prompt
    assert "python3.13" in tag_prompt
    assert "Few-shot examples:" not in runner.prompts["description"]
    assert "Tag rules:" not in runner.prompts["description"]


def test_regeneration_uses_configured_tag_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_config: Path,
) -> None:
    _write_jsonl(
        isolated_config.parent / "rss_examples.jsonl",
        [{"input": "Title: Past", "output": "<p>x</p>", "keywords": ["python", "asyncio"]}],
    )
    isolated_config.write_text(
        "tags:\n  exclude: [Programmierung]\n  examples: rss_examples.jsonl\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "episode"
    store = EpisodeWorkspaceStore(workspace)
    store.write_episode_yaml({"episode_id": "ep", "hosts": ["Johannes"]})
    summary_path = store.layout.episode_summary_json_path()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(EpisodeSummary(summary_markdown="S", key_points=[], topics=[]).model_dump_json())
    runner = Mock()
    runner.return_value.run.return_value = {
        "candidates": [{"content": "- Programmierung\n- Johannes\n- asyncio\n- trio"}],
    }
    monkeypatch.setattr("podcast_pipeline.drafter_runner.DrafterCliRunner", runner)

    candidates = generate_single_asset_candidates_llm(workspace=workspace, asset_id="cms_tags", candidates_per_asset=1)

    assert candidates[0].content == "- asyncio\n- trio"
    prompt = runner.return_value.run.call_args.args[0]
    assert "Title: Past" in prompt
    assert "- python\n- asyncio" in prompt


def test_build_tag_pool_orders_by_agreement_and_filters() -> None:
    contents = [
        "- Python Podcast\n- pypy\n- JIT\n- Jochen",
        "pypy, jit, free-threading",
        "- jit\n- PyPy\n- pypy\n- profiling",
    ]
    pool = build_tag_pool(contents, settings=TagSettings(max_tags=2), hosts=_HOSTS)
    assert [(entry.tag, entry.count) for entry in pool] == [
        ("pypy", 3),
        ("JIT", 3),
        ("free-threading", 1),
        ("profiling", 1),
    ]


def test_render_selected_tags_markdown() -> None:
    assert render_selected_tags_markdown("itunes_keywords", ["python", "jit"]) == (
        "# iTunes keywords\n\n- python\n- jit\n"
    )
    assert render_selected_tags_markdown("cms_tags", []) == "# CMS tags\n"


def test_load_episode_tag_settings_falls_back_on_invalid_config(tmp_path: Path, isolated_config: Path) -> None:
    isolated_config.write_text("tags:\n  max: nope\n", encoding="utf-8")
    store = EpisodeWorkspaceStore(tmp_path / "episode")
    store.write_episode_yaml({"episode_id": "ep", "hosts": ["Jochen"]})
    settings, hosts = load_episode_tag_settings(tmp_path / "episode")
    assert settings == TagSettings()
    assert hosts == ["Jochen"]


def test_load_episode_tag_settings_skips_examples(tmp_path: Path, isolated_config: Path) -> None:
    isolated_config.write_text("tags:\n  max: 9\n  examples: missing.jsonl\n", encoding="utf-8")
    settings, hosts = load_episode_tag_settings(tmp_path)
    assert settings.max_tags == 9
    assert settings.examples == ()
    assert hosts == []


def test_write_selected_tags_enforces_cap_and_clears_candidate_selection(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep"})
    candidate = Candidate(asset_id="cms_tags", content="- a\n- b")
    store.write_candidate(candidate)
    state = EpisodeWorkspace(
        episode_id="ep",
        root_dir=".",
        assets=[Asset(asset_id="cms_tags", candidates=[candidate], selected_candidate_id=candidate.candidate_id)],
    )

    unchanged, error = write_selected_tags(
        store=store, workspace_state=state, asset_id="cms_tags", tags=["a", "b", "c"], max_tags=2
    )
    assert error is not None and "At most 2" in error
    assert unchanged is state
    assert not store.layout.selected_text_path("cms_tags", TextFormat.markdown).exists()

    updated, error = write_selected_tags(
        store=store, workspace_state=state, asset_id="cms_tags", tags=["a", "A", "b"], max_tags=2
    )
    assert error is None
    assert updated.assets[0].selected_candidate_id is None
    assert store.read_state().assets[0].selected_candidate_id is None
    assert store.read_selected_text("cms_tags", TextFormat.markdown) == "# CMS tags\n\n- a\n- b\n"


def test_long_single_line_prose_is_dropped_not_split() -> None:
    prose = "A detailed discussion of the latest developments in Python today"
    settings = TagSettings()
    assert postprocess_tag_content("itunes_keywords", prose, settings=settings) == ""
    assert postprocess_tag_content("cms_tags", prose, settings=settings) == ""
    assert build_tag_pool([prose, "- jit"], settings=settings) == [TagPoolEntry(tag="jit", count=1)]


@pytest.mark.parametrize("bad_tag", ["a\n- b\n- c", "a\r\nb"])
def test_write_selected_tags_rejects_line_breaks(tmp_path: Path, bad_tag: str) -> None:
    store = EpisodeWorkspaceStore(tmp_path)
    store.write_episode_yaml({"episode_id": "ep"})
    state = EpisodeWorkspace(episode_id="ep", root_dir=".")
    unchanged, error = write_selected_tags(
        store=store, workspace_state=state, asset_id="cms_tags", tags=[bad_tag], max_tags=2
    )
    assert error is not None and "line breaks" in error
    assert unchanged is state
    assert not store.layout.selected_text_path("cms_tags", TextFormat.markdown).exists()
