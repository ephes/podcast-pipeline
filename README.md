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

## Domain models

Core Pydantic models live in `podcast_pipeline.domain` and are intended to back `episode.yaml` + `state.json`.

## Workspace publication and recovery

The review engine's planned protocol documents are all serialized before publication, then each JSON file is atomically
replaced. Agent CLI diagnostic creator files are written separately during generation.
`state.json` is updated only after protocol and copy artifacts have been published. A failed run may leave a mixture
of complete old and new protocol files; this is not a transaction across the bundle. Treat workspace state as the
last successful publication marker and rerun the review loop to rebuild a partial bundle.

Selected Markdown has a canonical `.md` file and derived `.html`; plain text and HTML selections have only their
canonical file. A successful format switch removes superseded files. New selection files are staged first; ordinary
publication or superseded-file cleanup failures restore changed files and leave selection metadata unchanged. If
rollback itself fails, the error names a retained `.selection-*` directory containing `.old` backups; restore those
files before consuming outputs. These multi-file changes are not crash-atomic: after interruption, repeat the selection
or recover retained backups before consuming outputs.

Transcript ingest opens the source before touching current artifacts and copies into a sibling staging file before
replacing `transcript.txt`. Missing/unreadable sources and copy/replace failures preserve the previous transcript,
chunks, summaries and episode metadata. Self paths, hard-link aliases and identical bytes are no-ops unless an earlier
ingest still needs invalidation. A `.ingest-pending` marker is published before replacement: chunks and summaries are
then removed, `episode.yaml` is updated, and the marker is removed last. If interrupted during invalidation or metadata
publication, rerunning `podcast draft` (with or without `--transcript`) completes those steps before drafting, including
when the supplied transcript has identical bytes or is the workspace file itself. Fix an invalid `episode.yaml` before
retrying. This ordering is recoverable rather than a transaction across all files.
