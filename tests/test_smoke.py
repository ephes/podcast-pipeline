from pathlib import Path

from podcast_pipeline import __version__
from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout


def test_version_is_set() -> None:
    assert __version__


def test_workspace_layout_is_deterministic(tmp_path: Path) -> None:
    layout = EpisodeWorkspaceLayout(root=tmp_path)
    assert layout.episode_yaml == tmp_path / "episode.yaml"
    assert layout.state_json == tmp_path / "state.json"
    assert layout.copy_candidates_dir == tmp_path / "copy" / "candidates"
