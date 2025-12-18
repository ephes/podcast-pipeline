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
    assert layout.copy_protocol_dir == tmp_path / "copy" / "protocol"


def test_fixture_files_exist() -> None:
    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "pp_068"
    assert (fixture_dir / "transcript.txt").exists()
    assert (fixture_dir / "chapters.txt").exists()
