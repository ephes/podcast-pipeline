from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from podcast_pipeline.entrypoints.cli import app
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def test_cli_init_creates_workspace_tree(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"

    result = runner.invoke(
        app,
        [
            "init",
            "--workspace",
            str(workspace),
            "--episode-id",
            "ep_001",
        ],
    )

    assert result.exit_code == 0, result.stdout

    store = EpisodeWorkspaceStore(workspace)
    assert store.read_episode_yaml()["episode_id"] == "ep_001"
    assert store.read_state().episode_id == "ep_001"

    expected_dirs = [
        workspace / "transcript",
        workspace / "transcript" / "chunks",
        workspace / "summaries",
        workspace / "summaries" / "chunks",
        workspace / "summaries" / "episode",
        workspace / "copy" / "candidates",
        workspace / "copy" / "reviews",
        workspace / "copy" / "selected",
        workspace / "copy" / "provenance",
        workspace / "copy" / "protocol",
        workspace / "auphonic",
        workspace / "auphonic" / "downloads",
        workspace / "auphonic" / "outputs",
    ]
    for path in expected_dirs:
        assert path.is_dir()


def test_cli_init_rejects_existing_workspace(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "keep").write_bytes(b"existing workspace")

    result = runner.invoke(
        app,
        [
            "init",
            "--workspace",
            str(workspace),
            "--episode-id",
            "ep_001",
        ],
    )

    assert result.exit_code != 0
    output = result.stdout + result.stderr
    assert "workspace already exists" in output
    assert (workspace / "keep").read_bytes() == b"existing workspace"


def test_cli_init_rejects_invalid_episode_id(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"

    result = runner.invoke(
        app,
        [
            "init",
            "--workspace",
            str(workspace),
            "--episode-id",
            "bad/id",
        ],
    )

    assert result.exit_code != 0
    output = result.stdout + result.stderr
    assert "episode_id" in output


@pytest.mark.parametrize("stage", ["dirs", "yaml", "state"])
def test_init_failure_cleans_owned_root_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    from podcast_pipeline.entrypoints import init

    workspace = tmp_path / "workspace"
    args = ["init", "--workspace", str(workspace), "--episode-id", "ep_001"]
    called = False

    def fail(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True
        raise OSError("injected failure")

    with monkeypatch.context() as patch:
        if stage == "dirs":
            patch.setattr(init, "_create_workspace_dirs", fail)
        else:
            patch.setattr(EpisodeWorkspaceStore, "write_episode_yaml" if stage == "yaml" else "write_state", fail)
        result = CliRunner().invoke(app, args)
    assert called
    assert result.exit_code != 0
    assert "failed to initialize workspace" in result.stdout + result.stderr
    assert "injected failure" in result.stdout + result.stderr
    assert not workspace.exists()
    assert CliRunner().invoke(app, args).exit_code == 0


def _init_args(workspace: Path) -> list[str]:
    return ["init", "--workspace", str(workspace), "--episode-id", "ep_001"]


def test_init_leaves_no_staging_directory_and_keeps_directory_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    reference = tmp_path / "reference"
    reference.mkdir()

    assert CliRunner().invoke(app, _init_args(workspace)).exit_code == 0

    assert sorted(path.name for path in tmp_path.iterdir()) == ["reference", "workspace"]
    assert workspace.stat().st_mode == reference.stat().st_mode


@pytest.mark.parametrize("stage", ["dirs", "yaml", "state"])
def test_init_failure_never_deletes_concurrently_added_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    from podcast_pipeline.entrypoints import init

    workspace = tmp_path / "workspace"

    def populate_then_fail(*args: object, **kwargs: object) -> None:
        # Another process starts using the claimed name while init is running.
        (workspace / "keep").write_bytes(b"untouched")
        raise OSError("injected failure")

    if stage == "dirs":
        monkeypatch.setattr(init, "_create_workspace_dirs", populate_then_fail)
    else:
        name = "write_episode_yaml" if stage == "yaml" else "write_state"
        monkeypatch.setattr(EpisodeWorkspaceStore, name, populate_then_fail)
    result = CliRunner().invoke(app, _init_args(workspace))

    assert result.exit_code != 0
    output = " ".join((result.stdout + result.stderr).replace("│", " ").split())
    assert "injected failure" in output
    assert "existing workspace content" in output
    assert (workspace / "keep").read_bytes() == b"untouched"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["workspace"]


def test_init_failure_preserves_replaced_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from podcast_pipeline.entrypoints import init

    workspace = tmp_path / "workspace"
    moved = tmp_path / "moved"

    def replace(layout: object) -> None:
        workspace.rename(moved)
        workspace.mkdir()
        (workspace / "keep").write_bytes(b"untouched")
        raise OSError("injected replacement")

    monkeypatch.setattr(init, "_create_workspace_dirs", replace)
    result = CliRunner().invoke(app, _init_args(workspace))

    assert result.exit_code != 0
    assert (workspace / "keep").read_bytes() == b"untouched"
    assert moved.is_dir()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["moved", "workspace"]


def test_init_does_not_overwrite_content_added_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    original = EpisodeWorkspaceStore.write_state

    def write_then_populate(self: EpisodeWorkspaceStore, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        (workspace / "keep").write_bytes(b"untouched")

    monkeypatch.setattr(EpisodeWorkspaceStore, "write_state", write_then_populate)
    result = CliRunner().invoke(app, _init_args(workspace))

    assert result.exit_code != 0
    assert "failed to initialize workspace" in result.stdout + result.stderr
    assert [path.name for path in workspace.iterdir()] == ["keep"]
    assert (workspace / "keep").read_bytes() == b"untouched"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["workspace"]


def test_init_reports_original_and_cleanup_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from podcast_pipeline.entrypoints import init

    workspace = tmp_path / "workspace"

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("state write failed")

    def cleanup_fail(*args: object, **kwargs: object) -> None:
        raise OSError("cleanup denied")

    monkeypatch.setattr(EpisodeWorkspaceStore, "write_state", fail)
    monkeypatch.setattr(init, "_empty_directory", cleanup_fail)
    result = CliRunner().invoke(app, _init_args(workspace))

    assert result.exit_code != 0
    # The error box wraps long lines; compare against the unwrapped text.
    output = " ".join((result.stdout + result.stderr).replace("│", " ").split())
    assert "state write failed" in output
    assert "cleanup denied" in output
    assert "staging directory left in place" in output
    # The empty claim is released even when the staging tree could not be removed.
    assert not workspace.exists()
    assert _staging_dirs(tmp_path)


def _staging_dirs(parent: Path) -> list[Path]:
    return [path for path in parent.iterdir() if path.name.startswith(".podcast-init-")]


@pytest.mark.parametrize("when", ["before_failure", "before_publication"])
def test_init_never_deletes_a_directory_swapped_into_the_staging_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    workspace = tmp_path / "workspace"
    stolen = tmp_path / "stolen"
    original = EpisodeWorkspaceStore.write_state

    def swap_staging(self: EpisodeWorkspaceStore, *args: object, **kwargs: object) -> None:
        # Another process moves our staging directory away and puts a populated
        # directory under the same name.
        if when == "before_publication":
            original(self, *args, **kwargs)  # type: ignore[arg-type]
        (staging,) = _staging_dirs(tmp_path)
        staging.rename(stolen)
        staging.mkdir()
        (staging / "keep").write_bytes(b"untouched")
        if when == "before_failure":
            raise OSError("injected failure")

    monkeypatch.setattr(EpisodeWorkspaceStore, "write_state", swap_staging)
    result = CliRunner().invoke(app, _init_args(workspace))

    assert result.exit_code != 0
    output = " ".join((result.stdout + result.stderr).replace("│", " ").split())
    assert "staging directory left in place" in output
    (replacement,) = _staging_dirs(tmp_path)
    assert (replacement / "keep").read_bytes() == b"untouched"
    # Only the directory this invocation created was emptied, through its descriptor.
    assert list(stolen.iterdir()) == []
    assert not workspace.exists()


def test_init_does_not_adopt_a_replaced_staging_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from podcast_pipeline.entrypoints import init

    workspace = tmp_path / "workspace"
    real_mkdtemp = init.tempfile.mkdtemp

    def mkdtemp_then_replace(prefix: str, dir: Path) -> str:
        created = Path(real_mkdtemp(prefix=prefix, dir=dir))
        created.rmdir()
        created.mkdir(mode=0o700)
        (created / "keep").write_bytes(b"untouched")
        return str(created)

    monkeypatch.setattr(init.tempfile, "mkdtemp", mkdtemp_then_replace)
    result = CliRunner().invoke(app, _init_args(workspace))

    assert result.exit_code != 0
    (replacement,) = _staging_dirs(tmp_path)
    assert [path.name for path in replacement.iterdir()] == ["keep"]
    assert (replacement / "keep").read_bytes() == b"untouched"
    assert not workspace.exists()


def test_init_accepts_workspace_name_at_the_filename_limit(tmp_path: Path) -> None:
    workspace = tmp_path / ("w" * 250)

    assert CliRunner().invoke(app, _init_args(workspace)).exit_code == 0
    assert EpisodeWorkspaceStore(workspace).read_state().episode_id == "ep_001"
