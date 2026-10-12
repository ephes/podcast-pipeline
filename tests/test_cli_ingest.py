from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from podcast_pipeline.entrypoints.cli import app
from podcast_pipeline.entrypoints.ingest import run_ingest
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def test_cli_ingest_updates_episode_yaml(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = EpisodeWorkspaceStore(workspace)
    store.write_episode_yaml({"episode_id": "ep_001", "inputs": {}})

    reaper_dir = tmp_path / "reaper"
    reaper_dir.mkdir()
    (reaper_dir / "Mic A.flac").write_text("a", encoding="utf-8")
    (reaper_dir / "Mic-A.flac").write_text("b", encoding="utf-8")
    (reaper_dir / "notes.txt").write_text("c", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "ingest",
            "--workspace",
            str(workspace),
            "--reaper-media-dir",
            str(reaper_dir),
            "--tracks-glob",
            "*.flac",
        ],
    )

    assert result.exit_code == 0, result.stdout

    updated = store.read_episode_yaml()
    assert updated["sources"]["reaper_media_dir"] == str(reaper_dir.resolve())
    assert updated["sources"]["tracks_glob"] == "*.flac"

    tracks = updated["tracks"]
    assert [track["path"] for track in tracks] == ["Mic A.flac", "Mic-A.flac"]
    assert [track["track_id"] for track in tracks] == ["mic_a", "mic_a_02"]


def test_cli_ingest_preserves_existing_track_metadata(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = EpisodeWorkspaceStore(workspace)
    store.write_episode_yaml(
        {
            "episode_id": "ep_001",
            "tracks": [
                {
                    "track_id": "host_main",
                    "path": "Mic A.flac",
                    "label": "Host",
                    "role": "host",
                    "channel": 1,
                },
            ],
        },
    )

    reaper_dir = tmp_path / "reaper"
    reaper_dir.mkdir()
    (reaper_dir / "Mic A.flac").write_text("a", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "ingest",
            "--workspace",
            str(workspace),
            "--reaper-media-dir",
            str(reaper_dir),
        ],
    )

    assert result.exit_code == 0, result.stdout

    updated = store.read_episode_yaml()
    track = updated["tracks"][0]
    assert track["track_id"] == "host_main"
    assert track["path"] == "Mic A.flac"
    assert track["label"] == "Host"
    assert track["role"] == "host"
    assert track["channel"] == 1


def test_cli_ingest_no_matches_preserves_yaml_bytes(tmp_path: Path) -> None:
    store = EpisodeWorkspaceStore(tmp_path / "workspace")
    store.write_episode_yaml(
        {
            "episode_id": "ep_001",
            "tracks": [
                {"track_id": "host", "path": "host.flac", "role": "host", "label": "Host"},
                {"track_id": "guest", "path": "guest.flac", "role": "guest", "label": "Guest"},
            ],
        }
    )
    before = store.layout.episode_yaml.read_bytes()
    media = tmp_path / "media"
    media.mkdir()
    result = CliRunner().invoke(
        app,
        ["ingest", "--workspace", str(store.layout.root), "--reaper-media-dir", str(media), "--tracks-glob", "*.wav"],
        env={"COLUMNS": "500"},
    )
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert str(media.resolve()) in result.output
    assert "*.wav" in result.output
    with pytest.raises(typer.BadParameter) as error:
        run_ingest(workspace=store.layout.root, reaper_media_dir=media, tracks_glob="*.wav")
    assert str(media.resolve()) in str(error.value)
    assert "*.wav" in str(error.value)
    assert store.layout.episode_yaml.read_bytes() == before
