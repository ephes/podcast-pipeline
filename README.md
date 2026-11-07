# podcast-pipeline

`podcast-pipeline` is an automated, multi-stage production pipeline that turns raw recordings and transcripts into reviewed episode copy, Auphonic outputs, and publish-ready assets.

## Development

Quality gates must pass before declaring work done:

```bash
uv sync
uv run playwright install chromium  # Linux CI: add --with-deps
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

### Subprocess output and transcription limits

Agent and transcription commands have independent 8 MiB stdout and 1 MiB stderr byte ceilings.
Overflow fails the step and kills/reaps the owned process group, as do timeout and interruption;
nonzero-exit diagnostics retain only the final 4096 characters. Normal stdout is preserved within the limit.
Transcription defaults to a finite 7200-second (two-hour) deadline; `podcast transcribe --timeout`
overrides it with finite positive seconds. This deadline is separate from the agent timeout.
Custom transcribers must write UTF-8, nonblank `transcript.txt` (and optionally `chapters.txt`) into
the supplied `--output-dir` / `{output_dir}`, which is a fresh temporary directory for each invocation.
Only successful fresh output is published; missing, empty or failed custom output keeps the previous transcript.
Custom wrappers must not hard-code the workspace transcript path.

### Dashboard request and history limits

Dashboard JSON requests are streamed with a 64 KiB body ceiling (413 on overflow), regardless of
Content-Length. Invalid UTF-8, malformed JSON or nesting beyond 64 levels returns 400 before work starts;
local host/origin checks run before body consumption. A worker launch failure records a failed job,
returns 503 and permits immediate retry while preserving single-flight admission.
Each job retains the latest 128 KiB / 1024 progress entries; oversized writes retain a UTF-8 tail.
JSON/SSE responses include an earlier-progress-truncated notice when history was evicted.
SSE progress IDs increase monotonically and support `Last-Event-ID` reconnects across eviction.
The dashboard retains at most 64 terminal jobs plus all running jobs; evicted job URLs return 404.

### Concurrent selections and RSS imports

Candidate, custom-text and curated-tag selections, dashboard deletion and review completion reload state
under the stable workspace state lock. Reviews needing human input keep the published selection.
Only the intended asset/selection provenance changes; its output files are published under the same lock.
Concurrent changes to different assets survive stale UI/CLI state, including Auphonic history.
For the same asset, the last writer to acquire the lock wins and its artifacts match its metadata.
RSS example imports stream at most 8 MiB of uncompressed input and share one whole-transfer deadline
across connection, headers, redirects and body reads (`--timeout-seconds`, default 20 seconds).
At most five redirects are followed without consuming intermediate bodies. Compressed feeds are refused;
fetch, HTTP, size or deadline errors preserve the previous atomic JSONL export.

### Dashboard drafts

The dashboard keeps unsaved editorial notes and custom text per asset while you switch assets,
refresh candidates or refine text. A save acknowledges only the submitted revision; newer edits
remain unsaved. Notes and custom-text save buttons are disabled while that asset has a save pending;
newer edits stay marked as unsaved and can be saved after it finishes.
Drafts live in this browser page only: save before closing or reloading it. Slow reads for an earlier
asset cannot replace the current editor.

`just test` includes headless Chromium tests of the shipped dashboard with synthetic API responses;
no episode data, subscription agents or Auphonic calls are used.

### Dashboard keyboard interaction

Dashboard section buttons are reachable with Tab and Shift-Tab; Enter or Space opens the named
section, and the pressed state identifies the current section. Only its panel is exposed.
Pick keeps focus on the equivalent candidate/tag/sidebar control after updates. Deleting a candidate
moves to the next candidate (or previous at the end), or the current asset heading when it disappears.
Background refreshes preserve the focused editor's latest text and caret. Enter in refinement/tag
inputs submits only committed text; composing IME Enter is left to the input method.

### Dashboard loading and retry

Browser API requests have a 30-second total deadline, including reading the response body.
A timed-out change has an unknown outcome: reload and check the stored selection or job state before
deliberately retrying. The dashboard never automatically repeats a mutation. Failed requests release
pending controls and preserve drafts; interrupted job streams can retry their health lookup.
Metadata shows loading/errors and requires a successfully loaded baseline before Save is available.
Use **Retry loading metadata** after a load error; it keeps keyboard focus through retry and
becomes **Reload metadata** after success. Typing during loading, including intentionally
clearing hosts, takes precedence over the loaded field values.

### Draft cache completion

Transcript chunks and episode summaries use `complete.json` manifests with SHA-256 file identities.
Drafting rebuilds legacy, interrupted, missing or changed sets instead of accepting a surviving prefix
or summary JSON alone. Chunk manifests bind the source transcript and record chunker settings;
summary manifests bind the chunk manifest and all three summary outputs. A contradictory LLM chunk ID
fails before that result is saved or reduced. See `docs/explanation/text-pipeline.md` for recovery limits.

Canonical transcription publication validates the complete UTF-8 output and atomically replaces each file.
Handled publication or metadata failures restore the previous canonical files, YAML and provenance.
A new transcript without chapters removes the canonical chapters and current-mode chapter pointers;
manually imported canonical chapters are retired too, so pass `draft --chapters` again to reuse them.
Other modes remain available. Content-bound draft caches rebuild for changed bytes and survive identical output.
