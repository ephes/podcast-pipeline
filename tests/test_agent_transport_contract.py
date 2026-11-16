from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from podcast_pipeline import agent_runners as runners


def run(script: str, text: str = "") -> subprocess.CompletedProcess[str]:
    return runners.run_cli_process([sys.executable, "-c", script], input=text, cwd=None, timeout=3)


def test_early_stdin_close_refuses_success() -> None:
    with pytest.raises(runners.AgentRunnerError, match="stdin"):
        run("import os; os.close(0); print('{\"done\": true}')", "x" * 1_000_000)


def test_surrogate_input_never_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("must admit encoding before launch")

    monkeypatch.setattr(runners.subprocess, "Popen", forbidden)
    with pytest.raises(runners.AgentRunnerError, match="input"):
        run("", "\ud800")


def test_large_complete_prompt() -> None:
    text = "Grüße" * 100_000
    assert run("import sys; sys.stdout.write(sys.stdin.read())", text).stdout == text


def test_success_retires_redirected_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "survived"
    helper = f"import time; from pathlib import Path; time.sleep(0.4); Path({str(marker)!r}).touch()"
    script = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {helper!r}], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print('{}')"
    )
    assert run(script).returncode == 0
    time.sleep(0.6)
    assert not marker.exists()


def test_group_signal_precedes_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    real_killpg = os.killpg
    signaled = False

    def signal_owned(pid: int, sig: int) -> None:
        nonlocal signaled
        assert process.returncode is None
        assert os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT).si_pid == pid
        signaled = True
        real_killpg(pid, sig)

    monkeypatch.setattr(runners.os, "killpg", signal_owned)
    runners._kill_process_group(process)
    assert signaled
    assert process.returncode is not None
