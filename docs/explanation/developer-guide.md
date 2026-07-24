# Developer guide

This guide orients contributors to the podcast pipeline architecture, how to extend providers,
and how to run the quality gates.

## Core architecture

The project is split into small modules that map to pipeline stages:

- `src/podcast_pipeline/entrypoints/`: CLI commands (`podcast ...`) that call the pipeline runners.
- `src/podcast_pipeline/workspace_store.py`: workspace layout and read/write helpers for `episode.yaml`,
  `state.json`, `copy/`, `transcript/`, and `summaries/`.
- `src/podcast_pipeline/domain/`: Pydantic models for `episode.yaml`, candidates, reviews, and workspace state.
- `src/podcast_pipeline/transcript_chunker.py`: chunk transcript text into overlapping segments.
- `src/podcast_pipeline/summarization_llm.py`: LLM-backed chunk and episode summaries.
- `src/podcast_pipeline/asset_candidates_llm.py`: LLM-backed generation of episode copy candidates.
- `src/podcast_pipeline/summarization_stub.py` and `asset_candidates_stub.py`: deterministic dry-run implementations.
- `src/podcast_pipeline/drafter_runner.py`: one-shot local CLI runner used by the text pipeline.
- `src/podcast_pipeline/review_loop_engine.py`: core creator/reviewer loop, produces protocol JSON.
- `src/podcast_pipeline/review_loop_orchestrator.py`: wiring around the loop, writes workspace artifacts.
- `src/podcast_pipeline/agent_runners.py`: CLI agent runners, prompt rendering, and fake runners for tests.

Typical production data flow:

1. `podcast transcribe` imports a transcript produced by the configured transcription command.
2. `podcast draft` chunks the transcript, creates LLM-backed summaries, and generates candidates.
3. `podcast pick` or `podcast dashboard` selects candidates for export and downstream use.
4. `podcast review` optionally runs the creator/reviewer loop for an asset.
5. `podcast produce` submits the selected copy and configured audio to Auphonic.

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
