# CLAUDE.md

**Note**: This project uses [bd (beads)](https://github.com/steveyegge/beads) for issue tracking. Use `bd` commands instead of markdown TODOs. See `AGENTS.md` for workflow details.

**IMPORTANT**: Do not run `git commit`, `git push`, or `bd sync` unless the user explicitly asks you to commit/push.

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`podcast-pipeline` is a local-first pipeline that generates and reviews episode copy (shownotes/description/announcements) from large transcripts using chunking + hierarchical summarization and a multi-model Creator/Reviewer loop (e.g. Codex + Claude).

Downstream steps (Auphonic, CMS draft updates, YouTube assets) are supported, but the MVP focuses on text automation.

## Project Structure

- `src/podcast_pipeline/` – library + CLI entrypoint
- `tests/` – pytest suite
- `specs/` – product/spec docs (PRD lives outside repo initially; link in `specs/README.md`)
- `pyproject.toml` / `uv.lock` – packaging + dependencies

## Common Commands

```bash
uv sync
just lint
just typecheck
just test
just bead
```

## Quality Gates (Required)

Do not declare a change finished unless these pass:

```bash
just lint
just typecheck
just test
```

## Beads (bd) Workflow

- Find work: `bd ready`
- Start: `bd update <id> --status in_progress`
- Context: `bd show <id> --json`, `bd dep tree <id>`
- Finish: `bd close <id> --reason "Done: <summary>"`
- Keep in sync: `bd sync`

## beadsflow (autopilot) integration (optional)

This repo can run an implementer↔reviewer epic loop using `beadsflow` with `beadsflow.toml`.

Recommended env:

```bash
export BEADS_NO_DAEMON=1
export BEADS_DIR="$PWD/.beads"
export BEADSFLOW_CONFIG="$PWD/beadsflow.toml"
```

Safe first run:

```bash
# Prefer running beadsflow from a local checkout (so bugs can be fixed directly):
uvx --from "$PWD/../beadsflow" beadsflow run <epic-id> --dry-run --verbose

# Convenience wrapper:
just beadsflow run <epic-id> --dry-run --verbose
```
