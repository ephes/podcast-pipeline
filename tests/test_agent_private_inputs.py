from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

import pytest

from podcast_pipeline import agent_runners as runners
from podcast_pipeline.agent_cli_config import AgentCliConfig


def test_prompt_file_fifo_rejected_without_wait(tmp_path: Path) -> None:
    fifo = tmp_path / "prompt"
    os.mkfifo(fifo)
    script = (
        "from pathlib import Path; from podcast_pipeline.agent_runners import _load_prompt_text,AgentRunnerError; "
        f"\ntry: _load_prompt_text(prompt_path=Path({str(fifo)!r}),prompt_text=None)"
        "\nexcept AgentRunnerError: pass\nelse: raise AssertionError('admitted FIFO')"
    )
    subprocess.run([sys.executable, "-c", script], timeout=2, check=True)


def test_prompt_bytes_admitted_before_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "prompt"
    path.write_text("x" * (1024 * 1024 + 1))
    with pytest.raises(runners.AgentRunnerError, match="prompt|input"):
        runners._load_prompt_text(prompt_path=path, prompt_text=None)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("excessive prompt must not launch")

    monkeypatch.setattr(runners.subprocess, "Popen", forbidden)
    with pytest.raises(runners.AgentRunnerError, match="input"):
        runners.run_cli_process(["synthetic"], input=path.read_text(), cwd=None, timeout=1)


def test_prompt_generation_change_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from podcast_pipeline import file_inputs

    path = tmp_path / "prompt"
    path.write_text("original")
    original_read = os.read

    def mutate(fd: int, size: int) -> bytes:
        part = original_read(fd, size)
        if part:
            path.write_text("replaced")
        return part

    monkeypatch.setattr(file_inputs.os, "read", mutate)
    with pytest.raises(runners.AgentRunnerError):
        runners._load_prompt_text(prompt_path=path, prompt_text=None)


def test_file_and_inline_prompt_agree(tmp_path: Path) -> None:
    path = tmp_path / "prompt"
    path.write_text("Grüße\n", encoding="utf-8")
    assert runners._load_prompt_text(prompt_path=path, prompt_text=None) == runners._load_prompt_text(
        prompt_path=None, prompt_text="Grüße\n"
    )


def test_failed_stderr_stays_private(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "SYNTHETIC_PRIVATE_SENTINEL\x1b[31m"

    def failed(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 7, "", secret)

    monkeypatch.setattr(runners, "run_cli_process", failed)
    with pytest.raises(runners.AgentRunnerError, match="exit code 7") as caught:
        runners.run_agent_cli(
            config=AgentCliConfig(role="creator", command="fake"),
            prompt_text="",
            cwd=None,
            timeout_seconds=1,
            label="Creator",
        )
    rendered = "".join(traceback.format_exception(caught.value))
    assert secret not in rendered
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in rendered
    assert "retry" in str(caught.value)


def test_malformed_json_stays_private() -> None:
    raw = '{"SYNTHETIC_PRIVATE_SENTINEL":}'
    with pytest.raises(runners.AgentRunnerError) as caught:
        runners.extract_json_payload(raw, label="Synthetic")
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in "".join(traceback.format_exception(caught.value))
