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

Auphonic gets the individual speaker tracks and mixes them itself: export one stem per speaker from
Reaper/Ultraschall (FLAC, synced, same length, trimmed as in the project) and list them in `episode.yaml`
(`auphonic.tracks`, or ingest them as `tracks`). A master mix is at most an input for transcription
(`inputs.transcription_audio`), never for Auphonic. The configured preset must be an Auphonic multitrack preset.

`podcast produce --workspace <ep>` creates an Auphonic multitrack production, uploads the stems (one form field per
track), starts it, polls it until Auphonic reports Done or a failure status, and downloads the outputs. Reruns resume
the production stored in `state.json`; `--restart` deliberately starts a new (paid) one after a failure, and
`--dry-run` only prints the payload, including the track list. `--update` re-runs the stored production without
uploading the stems again: it applies the current preset, metadata, chapters and per-track settings to it and starts it
again (`--update --dry-run` prints the update requests). Re-running a finished production costs credits again.
`state.json` records every production the workspace created; `podcast auphonic-cleanup` lists the ones a `--restart`
superseded and `--delete` removes them from the Auphonic account (irreversible, and only while the current production
is Done with downloaded outputs). `podcast produce --cleanup`, or `auphonic.cleanup_superseded: true`, does that
automatically after a successful run. Deleting the current production in the Auphonic web UI also deletes its stems:
`produce` and `--update` then stop and ask for `--restart`. Outputs are named after the episode (`output_basename`,
default `episode_id`). A single-file production is only available as an
explicit opt-in (`auphonic.mode: single_track`). Credentials: `AUPHONIC_API_KEY`, or `AUPHONIC_USER` +
`AUPHONIC_PASSWORD`. Details: `docs/tutorials/episode-workflow.md` ("Produce with Auphonic") and
`docs/decisions/auphonic-multitrack.md`.

## Episode draft on django-cast

`podcast cms-draft --workspace <ep>` creates or updates the episode page as an unpublished draft on a django-cast site
(for example python-podcast.de) through django-cast's editor API: it uploads the Auphonic MP3 as an Audio (title,
subtitle, chapter marks), creates the episode with the selected copy (overview = description plus an audio player
block, detail = shownotes), links the hosts, and prints the Wagtail preview and edit URLs. Re-runs update the same
draft (ids in `cms/draft.json`) and reuse the Audio; `--dry-run` prints the payloads without the token. It never
publishes: `podcast cms-publish` does that after a confirmation. Configure `cms.base_url` (https only), the podcast
page, the token's environment variable and the host → contributor mapping in the global config. The draft is meant
to end with a Voxhelm transcript with speaker labels: the Audio gets diarization mode Enabled and the episode its
contributors before the transcript is started. django-cast's API cannot yet add further formats to an Audio, upload
with diarization Enabled, set contributors or start a transcript, so the command ends with a numbered list of the
remaining Wagtail steps in order (M4A/Ogg/Opus, diarization mode, contributors, "Generate transcript") and uses each
endpoint automatically once django-cast offers it. Details:
`docs/tutorials/episode-workflow.md` ("Create the episode draft on the website"), `docs/reference/configuration.md`
and `docs/decisions/cms-draft.md`.

## Local web UIs

`podcast dashboard` and `podcast pick --web` listen on `127.0.0.1` only and reject requests from other origins or
hostnames, and state-changing requests without `Content-Type: application/json`. Dashboard jobs such as Auphonic
production run single-flight per episode. Details: `docs/tutorials/episode-workflow.md` ("Local web UIs").

## Custom selected text

Besides picking a whole generated candidate, `podcast pick --web` and the dashboard let you write or edit the
selected text of an asset (start from a candidate with "Edit", then "Use custom text"); tag assets use the tag pool
instead. Slugs are validated as
lowercase, hyphen-separated and URL-safe; custom selections count as selected for "Done", `podcast status` and
`podcast produce`. You can also refine a candidate or your own text with an instruction (`kürzer`, `ohne Emojis`):
the drafter agent rewrites it into a new candidate that records the instruction. Details:
`docs/tutorials/episode-workflow.md` ("Write or edit the selected text", "Refine a text with an instruction").

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
