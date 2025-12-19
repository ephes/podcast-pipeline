# podcast-pipeline

`podcast-pipeline` is an automated, multi-stage production pipeline that turns raw recordings and transcripts into reviewed episode copy, Auphonic outputs, and publish-ready assets.

## Development

```bash
uv sync
just lint
just typecheck
just test
```

## Docs

We use MkDocs with a minimal `mkdocs.yml` at the repo root. Preview locally with:

```bash
just docs
```

## Domain models

Core Pydantic models live in `podcast_pipeline.domain` and are intended to back `episode.yaml` + `state.json`.

Beads (issues):

```bash
bd onboard
just bead
```
