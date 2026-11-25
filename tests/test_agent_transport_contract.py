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
        assert runners._observe_cli_exit(pid, block=True).si_pid == pid
        signaled = True
        real_killpg(pid, sig)

    monkeypatch.setattr(runners.os, "killpg", signal_owned)
    runners._kill_process_group(process)
    assert signaled
    assert process.returncode is not None


def test_thread_construction_failure_reaps_child_and_closes_pipes(monkeypatch: pytest.MonkeyPatch) -> None:
    real_popen = subprocess.Popen
    real_thread = runners.threading.Thread
    children: list[subprocess.Popen[bytes]] = []
    threads: list[runners.threading.Thread] = []

    def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def construct_thread(**kwargs: Any) -> runners.threading.Thread:
        if threads:
            raise RuntimeError("synthetic thread construction failure")
        thread = real_thread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(runners.subprocess, "Popen", spawn)
    monkeypatch.setattr(runners.threading, "Thread", construct_thread)
    try:
        with pytest.raises(RuntimeError, match="synthetic thread construction failure"):
            run("import time; time.sleep(10)")
        (child,) = children
        assert child.returncode is not None
        assert child.stdin is not None and child.stdin.closed
        assert child.stdout is not None and child.stdout.closed
        assert child.stderr is not None and child.stderr.closed
        assert threads[0].ident is None
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
                child.wait(timeout=3)
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe is not None:
                    pipe.close()


def test_cleanup_is_finite_and_preserves_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    class Process:
        pid = 1234
        returncode = None
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        waited = False

        def kill(self) -> None:
            raise OSError("private signal failure")

        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            assert timeout is not None and 0 < timeout <= 2
            raise subprocess.TimeoutExpired("private wait", timeout)

    process = Process()
    monkeypatch.setattr(runners.subprocess, "Popen", lambda *a, **kw: process)

    def timeout(*args: Any) -> None:
        raise subprocess.TimeoutExpired("synthetic", 3)

    def failed_signal(*args: Any) -> None:
        raise PermissionError("private signal failure")

    monkeypatch.setattr(runners, "_wait_for_cli", timeout)
    monkeypatch.setattr(runners.os, "waitid", lambda *args: None)
    monkeypatch.setattr(runners.os, "killpg", failed_signal)
    with pytest.raises(subprocess.TimeoutExpired, match="synthetic") as caught:
        run("")
    assert process.waited
    assert any("cleanup uncertain" in note for note in caught.value.__notes__)
    assert process.stdout.closed and process.stderr.closed and process.stdin.closed


def test_lost_ownership_never_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait(timeout=3)

    def forbidden(*args: Any) -> None:
        pytest.fail("cannot signal after reaping the reserved leader")

    monkeypatch.setattr(runners.os, "killpg", forbidden)
    with pytest.raises(runners.AgentRunnerError, match="ownership"):
        runners._kill_process_group(process)


def test_missing_waitid_refuses_before_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("must not launch without waitid supervision")

    monkeypatch.delattr(runners.os, "waitid", raising=False)
    monkeypatch.setattr(runners.subprocess, "Popen", forbidden)
    with pytest.raises(runners.AgentRunnerError, match=r"Python 3\.13\+ on macOS"):
        run("")


def test_protocol_stdout_requires_exact_utf8() -> None:
    with pytest.raises(runners.AgentRunnerError, match="UTF-8"):
        run('import os; os.write(1, b\'{"content":"\\xff"}\')')


def test_non_protocol_stdout_keeps_replacement_decoding() -> None:
    result = runners.run_cli_process(
        [sys.executable, "-c", 'import os; os.write(1, b"progress \\xff done")'],
        input="",
        cwd=None,
        timeout=3,
        strict_stdout=False,
    )
    assert result.returncode == 0
    assert result.stdout == "progress \ufffd done"


def test_split_multibyte_stdout_is_preserved() -> None:
    assert run('import os; os.write(1,b"\\xc3"); os.write(1,b"\\xbc")').stdout == "ü"


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin libproc membership contract")
def test_darwin_permission_fallback_refuses_descendants() -> None:
    script = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)']);time.sleep(10)"
    )
    process = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        time.sleep(0.15)
        os.kill(process.pid, 9)
        runners._observe_cli_exit(process.pid, block=True)
        assert not runners._darwin_leader_only(process.pid)
    finally:
        runners._kill_process_group(process)


def test_interruption_retains_primary_and_cleans(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(*args: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(runners, "_wait_for_cli", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run("import time; time.sleep(10)")


def test_pipe_failure_cannot_race_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    from types import SimpleNamespace

    failures: list[BaseException] = []
    done = [threading.Event() for _ in range(3)]
    for event in done:
        event.set()

    def observe(*args: Any) -> object:
        failures.append(runners.AgentRunnerError("CLI stdin delivery failed"))
        return object()

    monkeypatch.setattr(runners.os, "waitid", observe)
    process: Any = SimpleNamespace(pid=1234)
    with pytest.raises(runners.AgentRunnerError, match="stdin"):
        runners._wait_for_cli(process, failures, done, ["synthetic"], 1, time.monotonic() + 1)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin libproc membership contract")
def test_darwin_permission_fallback_requires_exited_leader() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
    try:
        assert not runners._darwin_leader_only(process.pid)
    finally:
        runners._kill_process_group(process)


def test_external_reap_revokes_group_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    _, status = os.waitpid(process.pid, 0)

    def forbidden(*args: Any) -> None:
        pytest.fail("external reap revoked group ownership")

    monkeypatch.setattr(runners.os, "killpg", forbidden)
    try:
        with pytest.raises(runners.AgentRunnerError, match="ownership"):
            runners._kill_process_group(process)
    finally:
        process.returncode = os.waitstatus_to_exitcode(status)


@pytest.mark.parametrize("role", ["creator", "reviewer"])
@pytest.mark.parametrize("output", [b'{"content":"\xff"}', b'{"done":false,"done":true}'])
def test_invalid_protocol_never_publishes(tmp_path: Path, role: str, output: bytes) -> None:
    from podcast_pipeline.agent_cli_config import AgentCliConfig
    from podcast_pipeline.workspace_store import EpisodeWorkspaceLayout

    config = AgentCliConfig(role=role, command=sys.executable, args=("-c", f"import os; os.write(1,{output!r})"))
    cls = runners.CodexCliCreatorRunner if role == "creator" else runners.ClaudeCodeReviewerRunner
    runner = cls(layout=EpisodeWorkspaceLayout(root=tmp_path), config=config)
    with pytest.raises(runners.AgentRunnerError):
        runner.run_prompt(prompt_text="synthetic", asset_id="description", iteration=1)
    assert not list(tmp_path.rglob("*.json"))


def test_feeder_close_failure_is_reported_and_settled() -> None:
    import threading
    from typing import BinaryIO, cast

    class BrokenClose:
        def close(self) -> None:
            raise RuntimeError("synthetic private close detail")

    failures: list[BaseException] = []
    done = threading.Event()
    runners._feed_cli_pipe(cast(BinaryIO, BrokenClose()), b"", failures, done)
    assert done.is_set()
    assert len(failures) == 1 and "stdin close" in str(failures[0])
