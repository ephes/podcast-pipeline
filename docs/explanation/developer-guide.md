# Developer guide

This guide orients contributors to the podcast pipeline architecture, how to extend providers,
how to run the quality gates, and how we track work with Beads.

## Core architecture

The project is split into small modules that map to pipeline stages:

- `src/podcast_pipeline/entrypoints/`: CLI commands (`podcast ...`) that call the pipeline runners.
- `src/podcast_pipeline/workspace_store.py`: workspace layout and read/write helpers for `episode.yaml`,
  `state.json`, `copy/`, `transcript/`, and `summaries/`.
- `src/podcast_pipeline/domain/`: Pydantic models for `episode.yaml`, candidates, reviews, and workspace state.
- `src/podcast_pipeline/transcript_chunker.py`: chunk transcript text into overlapping segments.
- `src/podcast_pipeline/summarization_stub.py`: stub summarizer that creates chunk + episode summaries.
- `src/podcast_pipeline/asset_candidates_stub.py`: stub generator for draft copy assets.
- `src/podcast_pipeline/review_loop_engine.py`: core creator/reviewer loop, produces protocol JSON.
- `src/podcast_pipeline/review_loop_orchestrator.py`: wiring around the loop, writes workspace artifacts.
- `src/podcast_pipeline/agent_runners.py`: CLI agent runners, prompt rendering, and fake runners for tests.

Typical data flow (current MVP):

1. `podcast summarize` chunks the transcript and writes `summaries/` (stub today).
2. `podcast draft-candidates` generates asset candidates from the episode summary.
3. `podcast review` runs the creator/reviewer loop for one asset and writes protocol + selections.
4. `podcast pick` selects a candidate for export or downstream use.

For schema details, see:

- `../reference/episode-yaml.md`
- `../reference/review-protocol-schemas.md`

## Adding providers (creator/reviewer CLIs)

Creator and reviewer "providers" are CLI commands that accept a prompt on stdin and return JSON.
The JSON shapes are defined in `../reference/review-protocol-schemas.md`.

To add or swap a provider without code changes:

1. Install or expose the CLI.
2. Add the config in `~/.config/podcast-pipeline/config.yaml` or in a workspace `episode.yaml`:

```yaml
agents:
  creator:
    command: my-creator-cli
    args:
      - --format
      - json
  reviewer:
    command: my-reviewer-cli
    args:
      - --format
      - json
```

If a provider needs custom parsing or different prompt wiring:

- Add a new runner class in `src/podcast_pipeline/agent_runners.py`.
- Update `build_local_cli_runners` to select it (e.g., based on `AgentCliConfig.kind` or `command`).
- Extend `_DEFAULT_HINTS` in `src/podcast_pipeline/agent_cli_config.py` so `podcast status` can show install hints.

## Quality gates

Run these before declaring work done:

```bash
just lint
just typecheck
just test
```

If `just` is not available yet:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest
```

## Beads workflow

This repo uses a local Beads database under `.beads/`. To avoid accidentally using a global database:

```bash
export BEADS_NO_DAEMON=1
export BEADS_DIR="$PWD/.beads"
bd --no-daemon list
```

Recommended flow:

1. `bd ready`
2. `bd show <id> --json`
3. `bd update <id> --status in_progress`
4. Implement changes
5. `bd close <id> --reason "Done: <summary>"`

Beads is the source of truth for tracking; avoid markdown TODO lists for work items.
