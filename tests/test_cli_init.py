from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from podcast_pipeline.entrypoints.cli import app
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore


def _staging_dirs(parent: Path) -> list[Path]:
    return [path for path in parent.iterdir() if path.name.startswith(".podcast-init-")]


def _visible_names(parent: Path) -> list[str]:
    return sorted(path.name for path in parent.iterdir() if not path.name.startswith(".podcast-init-"))


def _output(result: object) -> str:
    # The error box wraps long lines; compare against the unwrapped text.
    return " ".join((result.stdout + result.stderr).replace("│", " ").split())  # type: ignore[attr-defined]


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
def test_init_failure_releases_claim_and_allows_retry(
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
    assert "failed to initialize workspace" in _output(result)
    assert "injected failure" in _output(result)
    assert not workspace.exists()
    assert len(_staging_dirs(tmp_path)) == 1
    assert CliRunner().invoke(app, args).exit_code == 0
    assert len(_staging_dirs(tmp_path)) == 1


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
    output = _output(result)
    assert "injected failure" in output
    assert "existing workspace content" in output
    assert (workspace / "keep").read_bytes() == b"untouched"
    assert _visible_names(tmp_path) == ["workspace"]


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
    assert _visible_names(tmp_path) == ["moved", "workspace"]


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
    assert _visible_names(tmp_path) == ["workspace"]


def test_init_failure_reports_and_keeps_the_staging_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("state write failed")

    monkeypatch.setattr(EpisodeWorkspaceStore, "write_state", fail)
    result = CliRunner().invoke(app, _init_args(workspace))

    assert result.exit_code != 0
    output = _output(result)
    assert "state write failed" in output
    (staging,) = _staging_dirs(tmp_path)
    assert f"partial workspace left in staging directory: {staging}".replace(" ", "") in output.replace(" ", "")
    # The partial tree is left for inspection; only the empty claim is released.
    assert (staging / "episode.yaml").exists()
    assert not workspace.exists()


def test_init_failure_keeps_files_added_to_an_adopted_staging_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from podcast_pipeline.entrypoints import init

    workspace = tmp_path / "workspace"
    real_mkdtemp = init.tempfile.mkdtemp

    def mkdtemp_then_replace(prefix: str, dir: Path) -> str:
        # Another process swaps in its own empty private directory before init opens it.
        created = Path(real_mkdtemp(prefix=prefix, dir=dir))
        created.rmdir()
        created.mkdir(mode=0o700)
        return str(created)

    def add_foreign_file_then_fail(*args: object, **kwargs: object) -> None:
        (staging,) = _staging_dirs(tmp_path)
        (staging / "keep").write_bytes(b"untouched")
        raise OSError("injected failure")

    monkeypatch.setattr(init.tempfile, "mkdtemp", mkdtemp_then_replace)
    monkeypatch.setattr(EpisodeWorkspaceStore, "write_state", add_foreign_file_then_fail)
    result = CliRunner().invoke(app, _init_args(workspace))

    assert result.exit_code != 0
    (staging,) = _staging_dirs(tmp_path)
    assert (staging / "keep").read_bytes() == b"untouched"
    assert not workspace.exists()


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
    output = _output(result)
    assert "left in staging directory" in output
    (replacement,) = _staging_dirs(tmp_path)
    assert (replacement / "keep").read_bytes() == b"untouched"
    # Nothing is deleted, not even the files this invocation wrote itself.
    assert (stolen / "episode.yaml").exists()
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
