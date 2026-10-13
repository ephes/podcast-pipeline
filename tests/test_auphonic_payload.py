from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from podcast_pipeline.auphonic_payload import AuphonicConfigError, build_auphonic_payload
from podcast_pipeline.domain.models import TextFormat
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


def _set_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: dict[str, Any]) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(config_path))


def _write_selected_text(workspace: Path, asset_id: str, content: str) -> None:
    layout = EpisodeWorkspaceLayout(root=workspace)
    path = layout.selected_text_path(asset_id, TextFormat.markdown)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.parametrize(
    ("auphonic_config", "expected_preset"),
    [
        ({"preset": "main"}, "preset_123"),
        ({"preset_id": "direct_456", "preset": "main"}, "direct_456"),
    ],
)
def test_payload_resolves_preset_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auphonic_config: dict[str, Any],
    expected_preset: str,
) -> None:
    _set_global_config(monkeypatch, tmp_path, {"auphonic": {"presets": {"main": "preset_123"}}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    payload = build_auphonic_payload(
        episode_yaml={"auphonic": auphonic_config},
        workspace=workspace,
    )

    assert payload["preset"] == expected_preset


@pytest.mark.parametrize(
    ("metadata_title", "selected_title", "expected_title"),
    [
        ("Explicit Title", "Selected Title", "Explicit Title"),
        (None, "Selected Title", "Selected Title"),
        (None, None, "ep_001"),
    ],
)
def test_payload_title_fallback_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_title: str | None,
    selected_title: str | None,
    expected_title: str,
) -> None:
    _set_global_config(monkeypatch, tmp_path, {"auphonic": {"presets": {"main": "preset_123"}}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    if selected_title is not None:
        _write_selected_text(workspace, "title_detail", f"# Heading\n{selected_title}\n")

    auphonic: dict[str, Any] = {"preset": "main"}
    if metadata_title is not None:
        auphonic["metadata"] = {"title": metadata_title}

    payload = build_auphonic_payload(
        episode_yaml={"episode_id": "ep_001", "auphonic": auphonic},
        workspace=workspace,
    )

    assert payload["metadata"]["title"] == expected_title


def test_payload_uses_preferred_track_roles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_global_config(monkeypatch, tmp_path, {"auphonic": {"presets": {"main": "preset_123"}}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_dir = tmp_path / "media"
    media_dir.mkdir()

    episode_yaml = {
        "episode_id": "ep_002",
        "sources": {"reaper_media_dir": str(media_dir)},
        "tracks": [
            {"path": "mix.wav", "role": "mix"},
            {"path": "raw.wav", "role": "dialog"},
        ],
        "auphonic": {"preset": "main"},
    }

    payload = build_auphonic_payload(episode_yaml=episode_yaml, workspace=workspace)

    assert payload["input_file"] == str((media_dir / "mix.wav").resolve())


def test_payload_normalizes_chapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_global_config(monkeypatch, tmp_path, {"auphonic": {"presets": {"main": "preset_123"}}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    episode_yaml = {
        "episode_id": "ep_003",
        "auphonic": {
            "preset": "main",
            "chapters": [
                "Intro",
                {"title": "Segment", "start": 12, "end_sec": "45.5"},
                {"title": "Outro", "start_sec": 90},
            ],
        },
    }

    payload = build_auphonic_payload(episode_yaml=episode_yaml, workspace=workspace)

    assert payload["chapters"] == [
        {"title": "Intro"},
        {"title": "Segment", "start": 12.0, "end": 45.5},
        {"title": "Outro", "start": 90.0},
    ]


def test_payload_requires_preset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_global_config(monkeypatch, tmp_path, {})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(AuphonicConfigError, match="Missing auphonic.preset"):
        build_auphonic_payload(episode_yaml={}, workspace=workspace)


def test_payload_rejects_invalid_global_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("auphonic: [", encoding="utf-8")
    monkeypatch.setenv("PODCAST_PIPELINE_CONFIG", str(config_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(AuphonicConfigError, match="Invalid YAML"):
        build_auphonic_payload(
            episode_yaml={"auphonic": {"preset": "main"}},
            workspace=workspace,
        )


def test_payload_parses_selected_itunes_keywords_from_markdown_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_global_config(monkeypatch, tmp_path, {"auphonic": {"presets": {"main": "preset_123"}}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_selected_text(
        workspace,
        "itunes_keywords",
        "# iTunes keywords\n\n- python\n- llm\n- agentic coding\n- devops\n",
    )

    payload = build_auphonic_payload(
        episode_yaml={"episode_id": "ep_004", "auphonic": {"preset": "main"}},
        workspace=workspace,
    )

    assert payload["metadata"]["itunes_keywords"] == "python, llm, agentic coding, devops"


def test_payload_rejects_multiple_input_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_global_config(monkeypatch, tmp_path, {})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(AuphonicConfigError, match="single input file"):
        build_auphonic_payload(
            episode_yaml={"auphonic": {"preset_id": "p1", "input_files": ["a.wav", "b.wav"]}},
            workspace=workspace,
        )


def test_payload_accepts_single_entry_input_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_global_config(monkeypatch, tmp_path, {})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    payload = build_auphonic_payload(
        episode_yaml={"auphonic": {"preset_id": "p1", "input_files": ["a.wav"]}},
        workspace=workspace,
    )

    assert payload["input_file"] == str((workspace / "a.wav").resolve())
    assert "input_files" not in payload


def test_payload_keeps_url_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_global_config(monkeypatch, tmp_path, {})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    for config in (
        {"preset_id": "p1", "input_file": "https://cdn.example/ep.mp3"},
        {"preset_id": "p1", "input_files": ["https://cdn.example/ep.mp3"]},
    ):
        payload = build_auphonic_payload(episode_yaml={"auphonic": config}, workspace=workspace)
        assert payload["input_file"] == "https://cdn.example/ep.mp3"


@pytest.mark.parametrize("key", ["start", "end", "start_sec", "end_sec"])
@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), "nan", "-inf", -1, True, False, 10**400, "1e999", "bad"]
)
def test_produce_rejects_invalid_chapter_times_before_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: object
) -> None:
    import typer

    from podcast_pipeline.entrypoints import produce

    _set_global_config(monkeypatch, tmp_path, {})
    (tmp_path / "episode.yaml").write_text(
        yaml.safe_dump(
            {"episode_id": "ep_001", "auphonic": {"preset_id": "p1", "chapters": [{"title": "A", key: value}]}}
        )
    )
    calls: list[bool] = []

    def credentials() -> Any:
        calls.append(True)
        raise AssertionError("API credentials must not be loaded")

    monkeypatch.setattr(produce, "load_auphonic_credentials", credentials)
    with pytest.raises(typer.BadParameter, match="finite nonnegative"):
        produce.run_produce(workspace=tmp_path, dry_run=False)
    assert calls == []


def test_payload_rejects_reversed_chapter_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_global_config(monkeypatch, tmp_path, {})
    with pytest.raises(AuphonicConfigError, match="end must be >= start"):
        build_auphonic_payload(
            episode_yaml={"auphonic": {"preset_id": "p1", "chapters": [{"title": "A", "start": 2, "end": 1}]}},
            workspace=tmp_path,
        )


@pytest.mark.parametrize("time", [0, "0", 0.5, "0.5"])
def test_payload_accepts_equal_nonnegative_chapter_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, time: object
) -> None:
    _set_global_config(monkeypatch, tmp_path, {})
    payload = build_auphonic_payload(
        episode_yaml={"auphonic": {"preset_id": "p1", "chapters": [{"title": "A", "start_sec": time, "end": time}]}},
        workspace=tmp_path,
    )
    assert payload["chapters"] == [{"title": "A", "start": float(str(time)), "end": float(str(time))}]


def test_store_preserves_invalid_chapters_without_widening_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import math

    from podcast_pipeline.workspace_store import EpisodeWorkspaceStore

    _set_global_config(monkeypatch, tmp_path, {})
    (tmp_path / "episode.yaml").write_text(
        "episode_id: ep_001\nauphonic:\n  preset_id: p1\n  metadata:\n    year: .nan\n"
        "  chapters:\n    - title: A\n      start: .nan\n"
    )
    episode = EpisodeWorkspaceStore(tmp_path).read_episode_yaml()
    assert math.isnan(episode["auphonic"]["chapters"][0]["start"])
    assert episode["auphonic"]["metadata"]["year"] is None
    episode["auphonic"]["chapters"] = []
    assert "year" not in build_auphonic_payload(episode_yaml=episode, workspace=tmp_path)["metadata"]
