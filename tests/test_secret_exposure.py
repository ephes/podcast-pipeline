"""Credentials never reach terminal output through crash reports or object reprs.

The CLI's crash report is checked in a child interpreter: Typer prints it from an
exception hook when the process ends, which an in-process ``CliRunner`` never runs.
Only fake credential values are used, and the Auphonic API is never called.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from podcast_pipeline.auphonic_api import AuphonicCredentials
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore

_FAKE_KEY = "fake-auphonic-key-7f3a9c"
_FAKE_PASSWORD = "fake-auphonic-password-1b8e"
_FAKE_CMS_TOKEN = "fake-cms-token-4d2e6a"

_CRASHING_PRODUCE = textwrap.dedent(
    """
    import sys

    import podcast_pipeline.entrypoints.produce as produce
    from podcast_pipeline.entrypoints.cli import main


    def crash(*_args, **_kwargs):
        raise RuntimeError("unexpected failure while producing")


    produce._produce = crash
    produce.build_auphonic_payload = lambda **_kwargs: {}
    sys.argv = ["podcast", "produce", "--workspace", sys.argv[1]]
    main()
    """
)


def _run_crashing_produce(tmp_path: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    workspace = tmp_path / "ws"
    EpisodeWorkspaceStore(workspace).write_episode_yaml({"episode_id": "ep1"})
    env = {k: v for k, v in os.environ.items() if not k.startswith("AUPHONIC_")}
    env.update(extra_env)
    env["COLUMNS"] = "240"
    return subprocess.run(
        [sys.executable, "-c", _CRASHING_PRODUCE, str(workspace)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )


def test_cli_crash_report_does_not_print_auphonic_api_key(tmp_path: Path) -> None:
    result = _run_crashing_produce(tmp_path, {"AUPHONIC_API_KEY": _FAKE_KEY})

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "unexpected failure while producing" in output
    assert _FAKE_KEY not in output


def test_cli_crash_report_does_not_print_auphonic_password(tmp_path: Path) -> None:
    result = _run_crashing_produce(tmp_path, {"AUPHONIC_USER": "someone", "AUPHONIC_PASSWORD": _FAKE_PASSWORD})

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "unexpected failure while producing" in output
    assert _FAKE_PASSWORD not in output


def test_cli_crash_report_does_not_print_cms_token(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import sys

        from podcast_pipeline.cms_api import load_cms_token
        from podcast_pipeline.entrypoints.cli import app

        @app.command("crash-with-token")
        def crash_with_token() -> None:
            token = load_cms_token("PODCAST_PIPELINE_TEST_CMS_TOKEN")
            raise RuntimeError(f"unexpected failure ({len(token)} characters)")

        sys.argv = ["podcast", "crash-with-token"]
        app()
        """
    )
    env = {**os.environ, "COLUMNS": "240", "PODCAST_PIPELINE_TEST_CMS_TOKEN": _FAKE_CMS_TOKEN}
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "unexpected failure" in output
    assert _FAKE_CMS_TOKEN not in output


def test_auphonic_credentials_repr_hides_secrets() -> None:
    text = repr(AuphonicCredentials(base_url="https://auphonic.test/api", api_key=_FAKE_KEY))
    basic = repr(AuphonicCredentials(base_url="https://auphonic.test/api", username="u", password=_FAKE_PASSWORD))

    assert _FAKE_KEY not in text
    assert _FAKE_PASSWORD not in basic
    assert "https://auphonic.test/api" in text
