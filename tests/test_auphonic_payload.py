from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from podcast_pipeline.auphonic_payload import AuphonicConfigError, build_auphonic_payload
from podcast_pipeline.domain.models import TextFormat
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout

# One speaker stem so payload tests that focus on metadata or presets build a valid multitrack production.
_STEMS: dict[str, Any] = {"tracks": [{"id": "jochen", "path": "stems/jochen.flac"}]}


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
        episode_yaml={"auphonic": {**auphonic_config, **_STEMS}},
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

    auphonic: dict[str, Any] = {"preset": "main", **_STEMS}
    if metadata_title is not None:
        auphonic["metadata"] = {"title": metadata_title}

    payload = build_auphonic_payload(
        episode_yaml={"episode_id": "ep_001", "auphonic": auphonic},
        workspace=workspace,
    )

    assert payload["metadata"]["title"] == expected_title


def test_payload_uses_episode_tracks_as_stems_and_skips_mix_tracks(
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
            {"track_id": "mix", "path": "mix.wav", "role": "mix"},
            {"track_id": "jochen", "path": "jochen.flac", "role": "host"},
            {"track_id": "dominik", "path": "dominik.flac"},
        ],
        "auphonic": {"preset": "main"},
    }

    payload = build_auphonic_payload(episode_yaml=episode_yaml, workspace=workspace)

    assert payload["is_multitrack"] is True
    assert "input_file" not in payload
    assert payload["multi_input_files"] == [
        {"type": "multitrack", "id": "jochen", "input_file": str((media_dir / "jochen.flac").resolve())},
        {"type": "multitrack", "id": "dominik", "input_file": str((media_dir / "dominik.flac").resolve())},
    ]


def test_payload_explicit_stems_override_episode_tracks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_global_config(monkeypatch, tmp_path, {})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    payload = build_auphonic_payload(
        episode_yaml={
            "episode_id": "ep_002",
            "tracks": [{"track_id": "raw", "path": "raw.flac"}],
            "auphonic": {
                "preset_id": "p1",
                "tracks": [
                    {"id": "Jochen", "path": "stems/jochen.flac", "algorithms": {"denoise": True}},
                    {"id": "Dominik", "path": "/abs/dominik.flac", "offset": 1.5},
                    {"id": "Johannes", "path": "https://cdn.example/johannes.flac"},
                ],
            },
        },
        workspace=workspace,
    )

    assert payload["multi_input_files"] == [
        {
            "type": "multitrack",
            "id": "Jochen",
            "input_file": str((workspace / "stems/jochen.flac").resolve()),
            "algorithms": {"denoise": True},
        },
        {"type": "multitrack", "id": "Dominik", "input_file": "/abs/dominik.flac", "offset": 1.5},
        {"type": "multitrack", "id": "Johannes", "input_file": "https://cdn.example/johannes.flac"},
    ]


@pytest.mark.parametrize("key", ["input_file", "input_files"])
def test_payload_rejects_master_mix_input_for_multitrack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    _set_global_config(monkeypatch, tmp_path, {})
    value: object = "final_mix.wav" if key == "input_file" else ["final_mix.wav"]

    with pytest.raises(AuphonicConfigError, match="inputs.transcription_audio") as excinfo:
        build_auphonic_payload(
            episode_yaml={"auphonic": {"preset_id": "p1", key: value, **_STEMS}},
            workspace=tmp_path,
        )
    assert f"auphonic.{key} is set" in str(excinfo.value)


def test_payload_requires_stems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_global_config(monkeypatch, tmp_path, {})

    with pytest.raises(AuphonicConfigError, match="No speaker stems"):
        build_auphonic_payload(
            episode_yaml={
                "tracks": [{"track_id": "mix", "path": "mix.wav", "role": "master"}],
                "auphonic": {"preset_id": "p1"},
            },
            workspace=tmp_path,
        )


@pytest.mark.parametrize(
    ("tracks", "message"),
    [
        ([{"id": "a", "path": "a.flac"}, {"id": "a", "path": "b.flac"}], "Duplicate Auphonic track id 'a'"),
        ([{"path": "a.flac"}], r"auphonic.tracks\[1\].id must be a non-empty string"),
        ([{"id": "a"}], r"auphonic.tracks\[1\].path must be a non-empty string"),
        ([{"id": "a", "path": "a.flac", "algorithms": "denoise"}], "algorithms must be a mapping"),
        ([{"id": "a", "path": "a.flac", "offset": -1}], "offset must be finite nonnegative"),
        ([{"id": "a", "path": "a.flac", "offset": True}], "offset must be finite nonnegative"),
        ("a.flac", "auphonic.tracks must be a list"),
    ],
)
def test_payload_validates_explicit_stems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracks: object, message: str
) -> None:
    _set_global_config(monkeypatch, tmp_path, {})

    with pytest.raises(AuphonicConfigError, match=message):
        build_auphonic_payload(episode_yaml={"auphonic": {"preset_id": "p1", "tracks": tracks}}, workspace=tmp_path)


def test_payload_rejects_unknown_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_global_config(monkeypatch, tmp_path, {})

    with pytest.raises(AuphonicConfigError, match="auphonic.mode must be one of"):
        build_auphonic_payload(
            episode_yaml={"auphonic": {"preset_id": "p1", "mode": "mixdown", **_STEMS}}, workspace=tmp_path
        )


@pytest.mark.parametrize(
    ("episode_yaml", "expected"),
    [
        ({"episode_id": "pp_72", "auphonic": {"preset_id": "p1", **_STEMS}}, "pp_72"),
        (
            {"episode_id": "pp_72", "auphonic": {"preset_id": "p1", "output_basename": " pp_72_final ", **_STEMS}},
            "pp_72_final",
        ),
        (
            {
                "episode_id": "pp_72",
                "auphonic": {"preset_id": "p1", "mode": "single_track", "input_file": "https://cdn.example/x.mp3"},
            },
            "pp_72",
        ),
        ({"auphonic": {"preset_id": "p1", **_STEMS}}, None),
    ],
)
def test_payload_names_outputs_after_the_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, episode_yaml: dict[str, Any], expected: str | None
) -> None:
    _set_global_config(monkeypatch, tmp_path, {})

    payload = build_auphonic_payload(episode_yaml=episode_yaml, workspace=tmp_path)

    assert payload.get("output_basename") == expected


@pytest.mark.parametrize("value", ["", "  ", "out/pp_72", "out\\pp_72", "..", 72])
def test_payload_rejects_invalid_output_basename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    _set_global_config(monkeypatch, tmp_path, {})

    with pytest.raises(AuphonicConfigError, match="auphonic.output_basename must be"):
        build_auphonic_payload(
            episode_yaml={"episode_id": "pp_72", "auphonic": {"preset_id": "p1", "output_basename": value, **_STEMS}},
            workspace=tmp_path,
        )


def test_dry_run_prints_full_multitrack_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from podcast_pipeline.entrypoints import produce

    _set_global_config(monkeypatch, tmp_path, {})
    (tmp_path / "episode.yaml").write_text(
        yaml.safe_dump(
            {
                "episode_id": "ep_001",
                "auphonic": {
                    "preset_id": "p1",
                    "chapters": [{"title": "Intro", "start": 0}, {"title": "Main", "start": 4168}],
                    "tracks": [
                        {"id": "jochen", "path": "stems/jochen.flac"},
                        {"id": "dominik", "path": "stems/dominik.flac", "algorithms": {"denoise": True}},
                    ],
                },
            }
        )
    )

    def credentials() -> Any:
        raise AssertionError("--dry-run must not load API credentials")

    monkeypatch.setattr(produce, "load_auphonic_credentials", credentials)
    produce.run_produce(workspace=tmp_path, dry_run=True)

    printed = json.loads(capsys.readouterr().out)
    assert printed["preset"] == "p1"
    assert printed["output_basename"] == "ep_001"
    assert printed["chapters"] == [
        {"title": "Intro", "start": "00:00:00.000"},
        {"title": "Main", "start": "01:09:28.000"},
    ]
    assert printed["is_multitrack"] is True
    assert printed["multi_input_files"] == [
        {"type": "multitrack", "id": "jochen", "input_file": str((tmp_path / "stems/jochen.flac").resolve())},
        {
            "type": "multitrack",
            "id": "dominik",
            "input_file": str((tmp_path / "stems/dominik.flac").resolve()),
            "algorithms": {"denoise": True},
        },
    ]


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
            **_STEMS,
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
        {"title": "Segment", "start": "00:00:12.000", "end": "00:00:45.500"},
        {"title": "Outro", "start": "00:01:30.000"},
    ]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "00:00:00.000"),
        (43, "00:00:43.000"),
        (59.999, "00:00:59.999"),
        ("12.25", "00:00:12.250"),
        (754.5, "00:12:34.500"),
        (3600, "01:00:00.000"),
        (4168, "01:09:28.000"),
        (4168.0, "01:09:28.000"),
        (3723.4567, "01:02:03.457"),
        (0.0004, "00:00:00.000"),
    ],
)
def test_payload_formats_chapter_times_for_auphonic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds: object, expected: str
) -> None:
    _set_global_config(monkeypatch, tmp_path, {})
    payload = build_auphonic_payload(
        episode_yaml={
            "auphonic": {
                "preset_id": "p1",
                **_STEMS,
                "chapters": [{"title": "A", "start": seconds, "end_sec": seconds}],
            }
        },
        workspace=tmp_path,
    )
    assert payload["chapters"] == [{"title": "A", "start": expected, "end": expected}]


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
        episode_yaml={"episode_id": "ep_004", "auphonic": {"preset": "main", **_STEMS}},
        workspace=workspace,
    )

    assert payload["metadata"]["itunes_keywords"] == "python, llm, agentic coding, devops"


def test_single_track_mode_rejects_multiple_input_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_global_config(monkeypatch, tmp_path, {})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(AuphonicConfigError, match="exactly one input file"):
        build_auphonic_payload(
            episode_yaml={"auphonic": {"preset_id": "p1", "mode": "single_track", "input_files": ["a.wav", "b.wav"]}},
            workspace=workspace,
        )


def test_single_track_mode_accepts_single_entry_input_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_global_config(monkeypatch, tmp_path, {})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    payload = build_auphonic_payload(
        episode_yaml={"auphonic": {"preset_id": "p1", "mode": "single_track", "input_files": ["a.wav"]}},
        workspace=workspace,
    )

    assert payload["input_file"] == str((workspace / "a.wav").resolve())
    assert "input_files" not in payload
    assert "is_multitrack" not in payload
    assert "multi_input_files" not in payload


def test_single_track_mode_requires_input_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_global_config(monkeypatch, tmp_path, {})

    with pytest.raises(AuphonicConfigError, match="single_track needs auphonic.input_file"):
        build_auphonic_payload(
            episode_yaml={"auphonic": {"preset_id": "p1", "mode": "single_track", **_STEMS}}, workspace=tmp_path
        )


def test_payload_keeps_url_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_global_config(monkeypatch, tmp_path, {})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    for config in (
        {"preset_id": "p1", "mode": "single_track", "input_file": "https://cdn.example/ep.mp3"},
        {"preset_id": "p1", "mode": "single_track", "input_files": ["https://cdn.example/ep.mp3"]},
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
        episode_yaml={
            "auphonic": {"preset_id": "p1", **_STEMS, "chapters": [{"title": "A", "start_sec": time, "end": time}]}
        },
        workspace=tmp_path,
    )
    expected = "00:00:00.500" if float(str(time)) else "00:00:00.000"
    assert payload["chapters"] == [{"title": "A", "start": expected, "end": expected}]


def test_store_preserves_invalid_chapters_without_widening_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import math

    from podcast_pipeline.workspace_store import EpisodeWorkspaceStore

    _set_global_config(monkeypatch, tmp_path, {})
    (tmp_path / "episode.yaml").write_text(
        "episode_id: ep_001\nauphonic:\n  preset_id: p1\n  tracks:\n    - id: jochen\n      path: jochen.flac\n"
        "  metadata:\n    year: .nan\n"
        "  chapters:\n    - title: A\n      start: .nan\n"
    )
    episode = EpisodeWorkspaceStore(tmp_path).read_episode_yaml()
    assert math.isnan(episode["auphonic"]["chapters"][0]["start"])
    assert episode["auphonic"]["metadata"]["year"] is None
    episode["auphonic"]["chapters"] = []
    assert "year" not in build_auphonic_payload(episode_yaml=episode, workspace=tmp_path)["metadata"]
