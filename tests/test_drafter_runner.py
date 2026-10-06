from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from podcast_pipeline import agent_runners
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
        cwd: str | None,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=stdout, stderr="")

    return fake_run


def test_run_parses_json_from_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"summary_markdown": "hello", "bullets": ["a", "b"]}
    monkeypatch.setattr(agent_runners, "run_cli_process", _fake_run_ok(json.dumps(expected)))
    runner = _make_runner()
    result = runner.run("test prompt")
    assert result == expected


def test_run_extracts_json_from_surrounding_text(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"key": "value"}
    raw_output = f"Some preamble text\n{json.dumps(expected)}\nSome trailing text"
    monkeypatch.setattr(agent_runners, "run_cli_process", _fake_run_ok(raw_output))
    runner = _make_runner()
    result = runner.run("test prompt")
    assert result == expected


def test_run_extracts_first_valid_json_when_trailing_text_contains_braces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"key": "value"}
    raw_output = (
        f"Some preamble text\n{json.dumps(expected)}\nSome trailing text with braces: {{not valid json object}}\n"
    )
    monkeypatch.setattr(agent_runners, "run_cli_process", _fake_run_ok(raw_output))
    runner = _make_runner()
    result = runner.run("test prompt")
    assert result == expected


def test_run_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        command: list[str],
        *,
        input: str,
        cwd: str | None,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="something went wrong")

    monkeypatch.setattr(agent_runners, "run_cli_process", fake_run)
    runner = _make_runner()
    with pytest.raises(AgentRunnerError, match="exit code 1"):
        runner.run("test prompt")


def test_run_raises_on_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_runners, "run_cli_process", _fake_run_ok(""))
    runner = _make_runner()
    with pytest.raises(AgentRunnerError, match="empty output"):
        runner.run("test prompt")


def test_run_raises_on_non_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_runners, "run_cli_process", _fake_run_ok("not json at all"))
    runner = _make_runner()
    with pytest.raises(AgentRunnerError, match="JSON"):
        runner.run("test prompt")


def test_run_passes_prompt_as_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_input: list[str] = []

    def fake_run(
        command: list[str],
        *,
        input: str,
        cwd: str | None,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]:
        captured_input.append(input)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(agent_runners, "run_cli_process", fake_run)
    runner = _make_runner()
    runner.run("my prompt text")
    assert captured_input == ["my prompt text"]


def test_run_raises_agent_runner_error_on_timeout() -> None:
    runner = _make_runner(command="sleep", args=("5",), timeout=0.2)
    with pytest.raises(AgentRunnerError, match=r"Drafter CLI timed out after 0\.2 s"):
        runner.run("test prompt")


def test_default_timeout_is_passed_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PODCAST_PIPELINE_AGENT_TIMEOUT", raising=False)
    seen: list[float | None] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs["timeout"])
        return subprocess.CompletedProcess(args=command, returncode=0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(agent_runners, "run_cli_process", fake_run)
    _make_runner().run("test prompt")
    monkeypatch.setenv("PODCAST_PIPELINE_AGENT_TIMEOUT", "42")
    _make_runner().run("test prompt")
    _make_runner(timeout=7).run("test prompt")
    assert seen == [900.0, 42.0, 7.0]


def test_timeout_kills_processes_spawned_by_the_cli(tmp_path: Path) -> None:
    """A helper the CLI started must not outlive the timeout and keep writing to the workspace."""
    marker = tmp_path / "late-write.txt"
    script = f"(sleep 1; echo late > {shlex.quote(str(marker))}) & sleep 30"
    runner = _make_runner(command="sh", args=("-c", script), timeout=0.3)
    with pytest.raises(AgentRunnerError, match="timed out"):
        runner.run("test prompt")
    time.sleep(1.5)
    assert not marker.exists()
