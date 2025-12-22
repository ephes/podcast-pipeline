from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class AgentCliConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentCliConfig:
    role: str
    command: str
    args: tuple[str, ...] = ()
    kind: str | None = None
    install_hint: str | None = None
    check_command: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class AgentCliBundle:
    creator: AgentCliConfig
    reviewer: AgentCliConfig


@dataclass(frozen=True)
class AgentCliIssue:
    role: str
    command: str
    message: str


_DEFAULT_HINTS: dict[str, tuple[str, str]] = {
    "codex": ("https://github.com/openai/codex#install", "codex --version"),
    "claude": ("https://github.com/anthropics/claude-code#install", "claude --version"),
}

_DEFAULT_CREATOR = AgentCliConfig(role="creator", command="codex", kind="codex")
_DEFAULT_REVIEWER = AgentCliConfig(role="reviewer", command="claude", kind="claude")


def global_config_path() -> Path:
    override = os.environ.get("PODCAST_PIPELINE_CONFIG")
    if override:
        return Path(override).expanduser()
    root = os.environ.get("XDG_CONFIG_HOME")
    if root:
        base = Path(root)
    else:
        base = Path.home() / ".config"
    return base / "podcast-pipeline" / "config.yaml"


def load_agent_cli_bundle(*, workspace: Path | None) -> AgentCliBundle:
    global_path = global_config_path()
    global_data = _load_yaml_mapping(global_path)
    episode_data: dict[str, Any] = {}

    episode_path: Path | None = None
    if workspace is not None:
        episode_path = workspace / "episode.yaml"
        if episode_path.exists():
            episode_data = _load_yaml_mapping(episode_path)

    global_agents = _extract_agents_section(global_data, source=global_path)
    episode_agents = _extract_agents_section(episode_data, source=episode_path or "episode.yaml")

    creator_raw, creator_source = _merge_agent_role(
        role="creator",
        global_role=global_agents.get("creator"),
        episode_role=episode_agents.get("creator"),
        global_source=str(global_path),
        episode_source=str(episode_path) if episode_path is not None else "episode.yaml",
    )
    reviewer_raw, reviewer_source = _merge_agent_role(
        role="reviewer",
        global_role=global_agents.get("reviewer"),
        episode_role=episode_agents.get("reviewer"),
        global_source=str(global_path),
        episode_source=str(episode_path) if episode_path is not None else "episode.yaml",
    )

    creator = _parse_agent_cli_config(
        role="creator",
        raw=creator_raw,
        fallback=_DEFAULT_CREATOR,
        source=creator_source,
    )
    reviewer = _parse_agent_cli_config(
        role="reviewer",
        raw=reviewer_raw,
        fallback=_DEFAULT_REVIEWER,
        source=reviewer_source,
    )
    return AgentCliBundle(creator=creator, reviewer=reviewer)


def collect_agent_cli_issues(*, workspace: Path | None) -> tuple[str, ...]:
    try:
        bundle = load_agent_cli_bundle(workspace=workspace)
    except AgentCliConfigError as exc:
        return (str(exc),)
    issues = _find_missing_cli_issues(bundle)
    return tuple(issue.message for issue in issues)


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AgentCliConfigError(f"Invalid YAML at {path}: {exc}") from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise AgentCliConfigError(f"Expected mapping YAML at {path}")
    return dict(loaded)


def _extract_agents_section(data: Mapping[str, Any], *, source: Path | str) -> dict[str, Mapping[str, Any]]:
    raw = data.get("agents")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise AgentCliConfigError(f"Expected mapping for agents in {source}")
    return {str(key): value for key, value in raw.items() if isinstance(key, str)}


def _merge_agent_role(
    *,
    role: str,
    global_role: Mapping[str, Any] | None,
    episode_role: Mapping[str, Any] | None,
    global_source: str,
    episode_source: str,
) -> tuple[Mapping[str, Any] | None, str]:
    merged: dict[str, Any] = {}
    source = "defaults"
    if global_role is not None:
        if not isinstance(global_role, Mapping):
            raise AgentCliConfigError(f"Expected mapping for agents.{role} in {global_source}")
        merged.update(dict(global_role))
        source = global_source
    if episode_role is not None:
        if not isinstance(episode_role, Mapping):
            raise AgentCliConfigError(f"Expected mapping for agents.{role} in {episode_source}")
        merged.update(dict(episode_role))
        source = episode_source
    return (merged if merged else None), source


def _parse_agent_cli_config(
    *,
    role: str,
    raw: Mapping[str, Any] | None,
    fallback: AgentCliConfig,
    source: str,
) -> AgentCliConfig:
    if raw is None:
        return fallback

    command = raw.get("command", fallback.command)
    if not isinstance(command, str) or not command.strip():
        raise AgentCliConfigError(f"agents.{role}.command must be a non-empty string in {source}")
    if any(ch.isspace() for ch in command):
        raise AgentCliConfigError(
            f"agents.{role}.command must not contain whitespace; use agents.{role}.args in {source}",
        )

    args = _parse_args(raw.get("args"), fallback=fallback.args, source=source, role=role)
    kind = _parse_optional_str(raw.get("kind"), fallback.kind, source=source, key=f"agents.{role}.kind")
    install_hint = _parse_optional_str(
        raw.get("install_hint"),
        fallback.install_hint,
        source=source,
        key=f"agents.{role}.install_hint",
    )
    check_command = _parse_optional_str(
        raw.get("check_command"),
        fallback.check_command,
        source=source,
        key=f"agents.{role}.check_command",
    )
    return AgentCliConfig(
        role=role,
        command=command,
        args=args,
        kind=kind,
        install_hint=install_hint,
        check_command=check_command,
        source=source,
    )


def _parse_optional_str(
    value: object,
    fallback: str | None,
    *,
    source: str,
    key: str,
) -> str | None:
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip():
        raise AgentCliConfigError(f"{key} must be a non-empty string in {source}")
    return value


def _parse_args(
    value: object,
    *,
    fallback: tuple[str, ...],
    source: str,
    role: str,
) -> tuple[str, ...]:
    if value is None:
        return fallback
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        if not all(isinstance(item, str) and item for item in value):
            raise AgentCliConfigError(f"agents.{role}.args must be a list of strings in {source}")
        return tuple(value)
    raise AgentCliConfigError(f"agents.{role}.args must be a list of strings in {source}")


def _find_missing_cli_issues(bundle: AgentCliBundle) -> tuple[AgentCliIssue, ...]:
    issues: list[AgentCliIssue] = []
    for config in (bundle.creator, bundle.reviewer):
        if _resolve_executable(config.command) is None:
            issues.append(
                AgentCliIssue(
                    role=config.role,
                    command=config.command,
                    message=_format_missing_cli_message(config),
                ),
            )
    return tuple(issues)


def _resolve_executable(command: str) -> Path | None:
    path = Path(command).expanduser()
    if path.parent != Path(".") or path.is_absolute():
        if path.exists() and os.access(path, os.X_OK):
            return path
        return None

    resolved = shutil.which(command)
    if resolved is None:
        return None
    return Path(resolved)


def _format_missing_cli_message(config: AgentCliConfig) -> str:
    install_hint, check_hint = _resolve_hints(config)
    source_note = f"Configured in {config.source}." if config.source else "Configured in defaults."
    global_path = global_config_path()
    update_note = f"Update agents.{config.role}.command in episode.yaml or {global_path}."
    lines = [
        f"Missing {config.role} CLI: `{config.command}`.",
        source_note,
        f"Install: {install_hint}",
        f"Check: {check_hint}",
        update_note,
    ]
    return "\n".join(lines)


def _resolve_hints(config: AgentCliConfig) -> tuple[str, str]:
    install_hint = config.install_hint
    check_hint = config.check_command
    if (install_hint is None or check_hint is None) and config.kind:
        defaults = _DEFAULT_HINTS.get(config.kind)
        if defaults:
            install_hint = install_hint or defaults[0]
            check_hint = check_hint or defaults[1]

    install_hint = install_hint or f"Ensure `{config.command}` is installed and on PATH."
    check_hint = check_hint or f"{config.command} --version"
    return install_hint, check_hint
