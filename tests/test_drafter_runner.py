from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from podcast_pipeline.agent_cli_config import AgentCliConfig
from podcast_pipeline.agent_runners import AgentRunnerError
from podcast_pipeline.drafter_runner import DrafterCliRunner


def _make_runner(
    *,
    command: str = "echo",
    args: tuple[str, ...] = (),
    timeout: float | None = None,
) -> DrafterCliRunner:
    config = AgentCliConfig(role="drafter", command=command, args=args)
    return DrafterCliRunner(config=config, timeout_seconds=timeout)


def _fake_run_ok(
    stdout: str,
) -> Any:
    def fake_run(
        command: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        check: bool,
        cwd: str | None,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=stdout, stderr="")

    return fake_run


def test_run_parses_json_from_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"summary_markdown": "hello", "bullets": ["a", "b"]}
    monkeypatch.setattr(subprocess, "run", _fake_run_ok(json.dumps(expected)))
    runner = _make_runner()
    result = runner.run("test prompt")
    assert result == expected


def test_run_extracts_json_from_surrounding_text(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"key": "value"}
    raw_output = f"Some preamble text\n{json.dumps(expected)}\nSome trailing text"
    monkeypatch.setattr(subprocess, "run", _fake_run_ok(raw_output))
    runner = _make_runner()
    result = runner.run("test prompt")
    assert result == expected


def test_run_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        command: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        check: bool,
        cwd: str | None,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="something went wrong")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = _make_runner()
    with pytest.raises(AgentRunnerError, match="exit code 1"):
        runner.run("test prompt")


def test_run_raises_on_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run_ok(""))
    runner = _make_runner()
    with pytest.raises(AgentRunnerError, match="empty output"):
        runner.run("test prompt")


def test_run_raises_on_non_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run_ok("not json at all"))
    runner = _make_runner()
    with pytest.raises(AgentRunnerError, match="JSON"):
        runner.run("test prompt")


def test_run_passes_prompt_as_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_input: list[str] = []

    def fake_run(
        command: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        check: bool,
        cwd: str | None,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]:
        captured_input.append(input)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = _make_runner()
    runner.run("my prompt text")
    assert captured_input == ["my prompt text"]
