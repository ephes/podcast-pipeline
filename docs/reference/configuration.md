# Configuration

## Agent CLI configuration (local only)

`podcast status` and `podcast review` resolve local CLI settings from either:

1. `episode.yaml` in the workspace root (per-episode overrides)
2. A global config at `~/.config/podcast-pipeline/config.yaml`
   (or the path in `PODCAST_PIPELINE_CONFIG`).

Example configuration:

```yaml
agents:
  creator:
    command: codex
    args:
      - --format
      - json
    kind: codex
    install_hint: https://github.com/openai/codex#install
    check_command: codex --version
  reviewer:
    command: claude
    args:
      - --format
      - json
    kind: claude
```

Notes:

- `command` must be a single executable (no whitespace). Put extra flags in `args`.
- `episode.yaml` overrides the global config when both are present.
- This configuration is for local CLI runners only; do not store secrets here.

### Agent CLI timeout

Each agent CLI call (drafter, creator and reviewer) runs with a per-call timeout. When the call exceeds it, the CLI
and any processes it started (its process group) are killed and the step fails with
`<Role> CLI timed out after N s`; a dashboard job then ends as `failed` and the stage accepts a new request. The
timeout is resolved in this order:

1. `podcast draft --timeout <seconds>` (or a positive `timeout` in the dashboard's `/api/draft` request body);
   `0` falls through to the next source.
2. The `PODCAST_PIPELINE_AGENT_TIMEOUT` environment variable (positive seconds).
3. `agents.<role>.timeout_seconds` in `episode.yaml` or the global config (positive seconds).
4. The default of 900 seconds (15 minutes).

Dashboard summarize, regenerate and draft (without a body `timeout`) use sources 2-4. Raise the value if a long
transcript legitimately needs more time per call:

```yaml
agents:
  drafter:
    command: claude
    timeout_seconds: 1800
```

## Auphonic presets (local only)

`podcast produce --dry-run` resolves Auphonic preset ids from the same config file. Define preset keys once and
reference them per episode:

```yaml
auphonic:
  presets:
    podcast_pipeline: "preset-uuid"
```

In `episode.yaml`:

```yaml
auphonic:
  preset: podcast_pipeline
```

Use `preset_id` in `episode.yaml` to skip mapping and set the preset id directly.

## episode.yaml metadata

`episode.yaml` also stores per-episode inputs, sources, and track metadata. See `reference/episode-yaml.md` for the full
schema and details on `podcast ingest`.
