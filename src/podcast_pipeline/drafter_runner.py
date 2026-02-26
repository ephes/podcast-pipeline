from __future__ import annotations

import subprocess
from typing import Any, Protocol, runtime_checkable

from podcast_pipeline.agent_cli_config import AgentCliConfig
from podcast_pipeline.agent_runners import AgentRunnerError, extract_json_payload


@runtime_checkable
class DrafterRunner(Protocol):
    """Protocol for any runner that takes a prompt and returns a parsed JSON dict."""

    def run(self, prompt_text: str) -> dict[str, Any]: ...


class DrafterCliRunner:
    """One-shot CLI runner for the drafter role.

    Pipes a prompt to the configured CLI command and parses JSON from stdout.
    Much simpler than the review-loop runners: no iteration state, no
    candidate writing.
    """

    def __init__(
        self,
        *,
        config: AgentCliConfig,
        timeout_seconds: float | None = None,
        cwd: str | None = None,
    ) -> None:
        self._config = config
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd

    def run(self, prompt_text: str) -> dict[str, Any]:
        """Run the CLI with *prompt_text* on stdin and return the parsed JSON dict."""
        raw = self._run_cli(prompt_text)
        return extract_json_payload(raw, label="Drafter")

    def _run_cli(self, prompt_text: str) -> str:
        command = [self._config.command, *self._config.args]
        result = subprocess.run(
            command,
            input=prompt_text,
            text=True,
            capture_output=True,
            check=False,
            cwd=self._cwd,
            timeout=self._timeout_seconds,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip()
            if detail:
                detail = f" {detail}"
            raise AgentRunnerError(f"Drafter CLI failed with exit code {result.returncode}.{detail}")
        if not (result.stdout or "").strip():
            raise AgentRunnerError("Drafter CLI returned empty output")
        return result.stdout or ""
