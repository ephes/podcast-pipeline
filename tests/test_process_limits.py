from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import typer

from podcast_pipeline import agent_runners as runners
from podcast_pipeline.entrypoints import transcribe


@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
def test_cli_output_limit(pipe: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runners, "CLI_STDOUT_LIMIT", 4096, raising=False)
    monkeypatch.setattr(runners, "CLI_STDERR_LIMIT", 4096, raising=False)
    with pytest.raises(runners.AgentRunnerError, match=f"{pipe} exceeded"):
        runners.run_cli_process(
            [sys.executable, "-c", f'import sys; sys.{pipe}.write("x" * 1000000); sys.{pipe}.flush()'],
            input="",
            cwd=None,
            timeout=5,
        )


def test_valid_protocol_output_unchanged() -> None:
    result = runners.run_cli_process(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        input='{"message": "Grüße"}\n',
        cwd=None,
        timeout=5,
    )
    assert result.stdout == '{"message": "Grüße"}\n'


class StalledProcess:
    pid = 12345
    returncode = None

    def __init__(self, *_args: Any, **kwargs: Any) -> None:
        assert kwargs["start_new_session"] is True
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.reaped = False

    def __enter__(self) -> StalledProcess:
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def poll(self) -> None:
        return None

    def communicate(self, input: object = None, timeout: float | None = None) -> tuple[str, str]:
        assert timeout is not None and timeout > 0
        raise subprocess.TimeoutExpired("synthetic", timeout)

    def kill(self) -> None:
        pass

    def wait(self) -> int:
        self.reaped = True
        return -9


@pytest.mark.parametrize("timeout", [None, 0.02])
def test_transcriber_deadline_kills_owned_group(
    timeout: float | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = StalledProcess(start_new_session=True)
    killed: list[int] = []
    monkeypatch.setattr(runners.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(runners.os, "killpg", lambda pid, sig: killed.append(pid))
    clock = iter([0.0, 0.0, 10000.0])
    monkeypatch.setattr(runners.time, "monotonic", lambda: next(clock, 10000.0))
    with pytest.raises(typer.BadParameter, match="timed out"):
        transcribe._run_transcriber(command="synthetic", args=[], cwd=tmp_path, timeout_seconds=timeout)
    assert killed == [process.pid]
    assert process.reaped


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_transcriber_invalid_deadline_before_launch(timeout: float, tmp_path: Path) -> None:
    with pytest.raises(typer.BadParameter, match="finite and positive"):
        transcribe.run_transcribe(
            workspace=tmp_path,
            mode=transcribe.TranscriptionMode.draft,
            config=transcribe.TranscribeConfig(command="synthetic", timeout_seconds=timeout),
        )


@pytest.mark.parametrize("output", [None, "", "   ", "fresh transcript"])
def test_custom_requires_fresh_output(
    output: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "episode.yaml").write_text("schema_version: 1\nepisode_id: synthetic\n")
    root = tmp_path / "transcript"
    mode_dir = root / "draft"
    mode_dir.mkdir(parents=True)
    for path in [mode_dir / "transcript.txt", root / "transcript.txt"]:
        path.write_text("previous transcript")

    def fake(**kwargs: Any) -> None:
        args = kwargs["args"]
        destination = Path(args[args.index("--output-dir") + 1])
        if output is not None:
            (destination / "transcript.txt").write_text(output)

    monkeypatch.setattr(transcribe, "_run_transcriber", fake)

    def run() -> None:
        transcribe.run_transcribe(
            workspace=tmp_path,
            mode=transcribe.TranscriptionMode.draft,
            config=transcribe.TranscribeConfig(command="synthetic"),
        )

    if output and output.strip():
        run()
        expected = output
    else:
        with pytest.raises(typer.BadParameter, match="Missing|empty"):
            run()
        expected = "previous transcript"
    assert (mode_dir / "transcript.txt").read_text() == expected
    assert (root / "transcript.txt").read_text() == expected
    assert not list(mode_dir.glob(".transcribe-*"))


def test_deadline_returns_when_helper_keeps_pipe_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a helper outside our kill boundary without any platform-specific signals.
    import time

    def kill_direct(process: subprocess.Popen[bytes]) -> None:
        process.kill()
        process.wait()

    monkeypatch.setattr(runners, "_kill_process_group", kill_direct)
    helper = "import time; time.sleep(3)"
    script = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{helper!r}]); time.sleep(30)'
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        runners.run_cli_process([sys.executable, "-c", script], input="", cwd=None, timeout=0.5)
    assert time.monotonic() - started < 2


def test_custom_staging_preserves_unrelated_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "episode.yaml").write_text("schema_version: 1\nepisode_id: synthetic\n")
    unrelated = str(tmp_path / "transcript" / "draft-notes")

    def fake(**kwargs: Any) -> None:
        args = kwargs["args"]
        assert args[-1] == unrelated
        output_dir = Path(args[args.index("--output-dir") + 1])
        (output_dir / "transcript.txt").write_text("fresh")
        (output_dir / "chapters.txt").write_bytes(b"\xff")

    monkeypatch.setattr(transcribe, "_run_transcriber", fake)
    with pytest.raises(typer.BadParameter, match="UTF-8"):
        transcribe.run_transcribe(
            workspace=tmp_path,
            mode=transcribe.TranscriptionMode.draft,
            config=transcribe.TranscribeConfig(command="synthetic", args=(unrelated,)),
        )
    assert not (tmp_path / "transcript" / "transcript.txt").exists()
    assert not (tmp_path / "transcript" / "draft" / "transcript.txt").exists()
    assert not (tmp_path / "transcript" / "draft" / "chapters.txt").exists()
    assert not list((tmp_path / "transcript" / "draft").glob(".transcribe-*"))
