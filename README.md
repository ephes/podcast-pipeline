# podcast-pipeline

`podcast-pipeline` is an automated, multi-stage production pipeline that turns raw recordings and transcripts into reviewed episode copy, Auphonic outputs, and publish-ready assets.

## Development

Quality gates must pass before declaring work done:

```bash
uv sync
just lint
just typecheck
just test
```

## Docs

We use Sphinx with MyST Markdown. Preview locally with:

```bash
just docs
```

For a static build:

```bash
just docs-build
```

The preview runs at http://127.0.0.1:8000 and static output lands in `docs/_build/html/`.

## Auphonic production

`podcast produce --workspace <ep>` creates and starts an Auphonic production (uploading a local input file), polls
it until Auphonic reports Done or a failure status, and downloads the outputs. Reruns resume the production stored in
`state.json`; `--restart` deliberately starts a new (paid) one after a failure, and `--dry-run` only prints the payload.
Credentials: `AUPHONIC_API_KEY`, or `AUPHONIC_USER` + `AUPHONIC_PASSWORD`. Details:
`docs/tutorials/episode-workflow.md` ("Produce with Auphonic").

## Local web UIs

`podcast dashboard` and `podcast pick --web` listen on `127.0.0.1` only and reject requests from other origins or
hostnames, and state-changing requests without `Content-Type: application/json`. Dashboard jobs such as Auphonic
production run single-flight per episode. Details: `docs/tutorials/episode-workflow.md` ("Local web UIs").

## Agent CLI timeout

Every agent CLI call (drafter, creator, reviewer) is killed after 15 minutes by default, so a CLI that hangs on a
login prompt, network stall or rate limit fails its dashboard job (which can then be retried) instead of keeping the
stage "running" until the dashboard restarts. Raise or lower it with `PODCAST_PIPELINE_AGENT_TIMEOUT=<seconds>` or
`agents.<role>.timeout_seconds` in the agent config; `podcast draft --timeout <seconds>` overrides both for one run.
Details: `docs/reference/configuration.md`.

## Tag assets

Generated `cms_tags`, `audio_tags` and `itunes_keywords` candidates target 6-10 specific terms and are post-filtered:
host names, show-level constants (`Python Podcast`, `Deutsch`, ...) and a configurable `tags.exclude` list are dropped,
and the count is capped (`tags.max`, default 12). `tags.examples` can point at a `podcast rss-examples` JSONL export so
past episodes' keywords serve as few-shot examples. `podcast pick --web` and the dashboard merge all tag candidates
into one pool ordered by agreement, where you toggle individual tags instead of picking a whole list (at most
`tags.max`); tag assets never block "Done". Details: `docs/reference/configuration.md` ("Tag assets").

## Domain models

Core Pydantic models live in `podcast_pipeline.domain` and are intended to back `episode.yaml` + `state.json`.

Chapter times are finite, nonnegative seconds. An optional `end_sec` must exceed `start_sec`.
Infinity and NaN are rejected on construction, JSON loading and assignment, so accepted chapter times
round-trip through JSON without becoming null.
