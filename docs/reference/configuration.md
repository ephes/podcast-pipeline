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
- Explicit command paths must be executable regular files; symlinks to such files are supported.
  Bare command names are resolved on PATH. Directories and nonexecutable files fail preflight.
- Missing agent configuration files use defaults. For agent settings, directories, unreadable files
  and invalid UTF-8 produce controlled diagnostics naming the path without exposing file contents.
- `episode.yaml` overrides the global config when both are present.
- This configuration is for local CLI runners only; do not store secrets here.

### Agent CLI timeout

Each agent CLI call (drafter, creator and reviewer) runs with a per-call timeout. When the call exceeds it, the CLI
and any processes it started (its process group) are killed and the step fails with
`<Role> CLI timed out after N s`; a dashboard job then ends as `failed` and the stage accepts a new request. The
timeout is resolved in this order:

1. `podcast draft --timeout <seconds>`. Explicit CLI values must be finite and positive;
   infinity, NaN, zero and negatives fail before workspace changes or agent launch, including dry runs.
   Omit the option to use the next source. The dashboard also uses a positive `/api/draft` body `timeout`;
   omitted, zero and negative body values use the next source. NaN and positive infinity body values fail the job before agent launch.
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

Productions are multitrack (Auphonic mixes the speaker stems), so the preset must be an Auphonic **multitrack preset**:
create it in Auphonic's multitrack mode (the API reports `is_multitrack: true`). Single-track presets do not apply to
multitrack productions; `podcast produce` reads the preset before creating a production and stops on a mismatch. Keep a
single-track preset only for episodes that opt into `auphonic.mode: single_track`.

A multitrack preset must not contain speaker tracks. Auphonic appends a preset's `multi_input_files` to the
production's, so a preset created with tracks (for example `jochen`, `dominik`) doubles them and the start fails with
"Multiple tracks with ID ... found". Keep the preset to master settings (output files, loudness, leveler, metadata
defaults; intro/outro entries are fine) and put per-track algorithms into `auphonic.tracks[].algorithms` in
`episode.yaml`. `podcast produce` stops before creating a production when the preset contains speaker tracks.

After changing a preset (for example adding Ogg Vorbis or audiogram outputs), `podcast produce --update` applies it to
the production stored in `state.json` and starts that production again without uploading the stems; re-running a
finished production costs Auphonic credits again. Auphonic re-applies the preset on that update, so the production's
output files become exactly the preset's. The same preset checks run before the update. See "Produce with Auphonic"
in `tutorials/episode-workflow.md`.

The preset checks fail closed. The preset response must carry preset data (a mapping); `is_multitrack` must be
`true` or `false`, and a preset without the field counts as a legacy single-track preset (presets from before
Auphonic's multitrack mode). Any other value, including `null`, stops the run. A multitrack preset's
`multi_input_files` may be missing, `null` or a list of track mappings whose `type`, when present, is text; anything
else stops the run as well. These checks run before the create request, and on `--update` before the first request
that changes the production, so a preset that cannot be checked never costs credits or touches the stored
production and its recorded downloads.

### Auphonic control response limits

Every Auphonic JSON API response (preset reads, create, upload, start, update, delete and status polls) is streamed
and capped separately at 8 MiB on the wire and 8 MiB after decoding (`auphonic_api.CONTROL_MAX_BYTES`).
Library callers can set both ceilings with `AuphonicClient(control_max_bytes=...)`. Control requests advertise
only `gzip, deflate` and inflate compressed bodies incrementally. Other content encodings, invalid or truncated
compressed bodies and trailing bytes fail. Redirects are followed without reading their bodies.
The parser requires strict UTF-8 JSON without a byte order mark. Excessive nesting, duplicate object members
at any depth, non-finite numbers (`NaN`, `Infinity`, `1e999`) and integers longer than Python's digit limit
raise an Auphonic API error. Oversized error responses also fail admission, including oversized 404 responses.
An empty successful DELETE response is accepted. A failure after a production was created still reports its UUID, which `state.json` already holds, and
never triggers a second create or start.

### Cleanup of superseded productions

`auphonic.cleanup_superseded` (default `false`) makes `podcast produce` delete the workspace's superseded Auphonic
productions (for example the ones `--restart` replaced) after the new production finished and its outputs were
downloaded. Deletion is irreversible, so it is opt-in:

```yaml
auphonic:
  cleanup_superseded: true
```

The key can be set in the global config and in `episode.yaml`; the value from `episode.yaml` wins. It must be `true` or
`false`. `podcast produce --cleanup` / `--no-cleanup` overrides it for one run, and
`podcast auphonic-cleanup [--dry-run | --delete]` lists or deletes them on demand. Only productions recorded as
superseded in the workspace's `state.json` are ever deleted, never the current one. See "Clean up superseded
productions" in `tutorials/episode-workflow.md`.

## Tag assets (local only)

`cms_tags`, `audio_tags` and `itunes_keywords` follow the show's tag house style: 6-10 short, specific terms
(technologies, projects, libraries, PEPs, Python versions), no show-level constants, no language tags, no host or guest
names and no generic summary phrases. The drafter prompt states these rules, and every generated tag candidate (from
`podcast draft` and dashboard regeneration) then passes a deterministic post-filter that:

- drops tags matching the exclude list (case-insensitive; spaces, hyphens and underscores are equivalent, a leading `#`
  is ignored). Built-in defaults: `podcast`, `python podcast`, `pythonpodcast`, `deutsch`, `german`, `englisch`,
  `english`;
- drops host names from `episode.yaml` `hosts` (`--host`): the full name and any tag containing one of its words of
  three or more letters, so `Dominik Geldmacher` also removes `Dominik`;
- drops entries with more than four words (prose rather than tags) and duplicates. A long line without separators
  stays one entry, so generated prose is dropped instead of being split into single-word tags;
- keeps at most `tags.max` tags (default 12).

Candidates are rewritten in their canonical format (`- tag` bullets for `cms_tags`/`audio_tags`, one comma-separated
line for `itunes_keywords`). Tags you add by hand while picking are not filtered.
Curated tags must fit on one line, including under Unicode line splitting. The picker
checks that the normalized list reads back exactly before it replaces the saved selection.
Each curated `itunes_keywords` entry must be comma-free because django-cast and Auphonic
export these entries as comma-delimited text. If export rejects an older selection, edit
any affected keyword in the picker or split it into separate entries. Comma-separated
keyword lines still represent separate entries. `cms_tags` and `audio_tags` export as arrays
and can include commas within a single tag.

When picking (`podcast pick --web` and the dashboard), each tag asset shows one suggestion pool instead of candidate
lists: the union of all its candidates, filtered with the same rules (without the per-candidate cap) and ordered by how
many candidates proposed each tag, ties in first-seen order. `tags.max` also caps the curated selection: saving more
entries is rejected with `400` (`At most N tags can be selected ...`), and so is a tag containing a line break. The default of 12 matches Apple's historical
limit for iTunes keywords. The picking UIs read the `tags` settings without loading `tags.examples`, and fall back to
the defaults when the tag configuration is invalid.

```yaml
tags:
  exclude:            # added to the built-in defaults
    - Programmierung
    - Softwareentwicklung
    - Johannes        # e.g. a regular guest
  max: 12             # hard cap per candidate (positive integer)
  examples: datasets/rss_examples.jsonl
  examples_limit: 5   # past episodes shown per tag prompt (positive integer)
```

`examples` points to a few-shot JSONL dataset from `podcast rss-examples` (uses `itunes:keywords`) or
`podcast cms-examples` (uses `tags`; `cms_tags` prefers CMS tags, the other tag assets prefer keywords). A relative path
resolves against the directory of the file that sets it. For each tag asset the pipeline picks up to `examples_limit`
past episodes whose tags best match the episode topics, applies the same filter to their tags and appends them to the
prompt as few-shot examples. Records without tags are ignored. A missing or invalid dataset fails `podcast draft`
before any agent runs.

The `tags` section can live in the global config and in `episode.yaml`: `exclude` lists are combined, while `max`,
`examples` and `examples_limit` from `episode.yaml` override the global values.

## django-cast episode drafts (cms)

`podcast cms-draft` and `podcast cms-publish` read a `cms` section from the global config. A `cms` section in
`episode.yaml` overrides single keys for one episode (`host_links` and `contributors` entries are merged).

```yaml
cms:
  base_url: https://python-podcast.de/      # required, https only
  token_env: PYTHON_PODCAST_API_TOKEN       # env var holding the API token (default PODCAST_PIPELINE_CMS_TOKEN)
  parent_id: 120                            # podcast page for new episodes
  parent_title: Python Podcast              # alternative to parent_id: match the podcast by title
  audio_collection: 1                       # Wagtail collection for the uploaded Audio
  explicit: 2                               # 1 = yes, 2 = no (default), 3 = clean
  detail_intro_html: "<h2>Shownotes</h2><p>…</p>"   # prepended to the shownotes (default "<h2>Shownotes</h2>")
  host_links:                               # first unlinked mention of each episode host in the overview is linked
    Jochen: https://fedi.wersdoerfer.de/@jochen
  contributors:                             # episode host -> django-cast Contributor slug
    Jochen: jochen                          # role host
    Gast: {slug: gast, role: guest}         # usually per episode, in episode.yaml
  transcript_diarization: enabled           # Audio transcript_diarization_mode: enabled (default), disabled, inherit
```

| Key | Where | Meaning |
| --- | --- | --- |
| `base_url` | global or episode | Site root of the django-cast installation; must be `https://` without credentials, query or fragment. All API requests go to `<base_url>api/editor/...`; redirects are never followed. |
| `token_env` | global or episode | Name of the environment variable with the DRF token, sent as `Authorization: Token <key>`. The token belongs in the environment, never in the config file. The token's user needs Wagtail admin access and add/edit permission on the podcast; `cms-publish` also needs publish permission (and the `publish` scope for a scoped token). |
| `parent_id` | global or episode | Id of the podcast page (`cast.Podcast`) episodes are created under; it must be listed by `GET api/editor/parents/` for the token. Without `parent_id` and `parent_title` the token must see exactly one podcast. |
| `parent_title` | global or episode | Selects the podcast by its exact title instead of the id. |
| `audio_collection` | global or episode | Collection id for the Audio upload; required by django-cast when the user can upload into more than one collection. |
| `explicit` | global or episode | iTunes explicit flag of the episode. |
| `detail_intro_html` | global or episode | HTML placed before the shownotes in the detail section. |
| `host_links` | global and episode (merged) | Host name → profile URL. Only hosts in `hosts` of `episode.yaml` are linked (all configured names when `hosts` is unset); names already linked, or whose URL is already in the overview, are left alone. |
| `contributors` | global and episode (merged) | Host name → Contributor slug (role `host`) or `{slug, role}` with role `host` or `guest`. The episode's `hosts` are credited in that order. A host without an entry, or an episode without `hosts`, is listed as a manual step, and the transcript is not started automatically. Sent to django-cast once its editor API accepts episode contributors. |
| `transcript_diarization` | global or episode | Diarization mode of the uploaded Audio. `enabled` (default) gives speaker labels regardless of the site's Voxhelm settings; `inherit` follows them. The transcript is only started automatically when the Audio reports `enabled`. |
| `audio_file` | episode only | File to upload as the Audio (relative to the workspace or absolute; `.mp3`, `.m4a`, `.ogg`/`.oga` or `.opus`). Default: `auphonic/outputs/<output_basename>.mp3`, else the only MP3 there. Files with the same name and another of these extensions are the further formats. |

`cms/draft.json` in the workspace stores `base_url`, `parent_id`, `episode_id`, `audio_id`, `audio_edit_url`, `audio_attached`
(false between an upload and the episode request that attaches it) and `transcript_pending` (an uploaded Audio whose
transcript was neither started nor listed as a manual step yet) after each step; ids stored for another `base_url`
are ignored. For an intentional reset, follow the backup-first instructions under "CMS recovery checkpoints" below; preserve any unattached Audio ID before abandoning reuse. See
"Create the episode draft on the website" in `tutorials/episode-workflow.md` for the requests and the current API
limitations.

## episode.yaml metadata

`episode.yaml` also stores per-episode inputs, sources, and track metadata. See `reference/episode-yaml.md` for the full
schema and details on `podcast ingest`.

## Concurrent episode edits

Episode YAML updates from the dashboard, ingest, drafting and transcription hold the
workspace lock across reading the latest document and atomically saving changes.
Independent note and track updates therefore survive concurrent operations.
Programmatic read-modify-write callers should use `store.update_episode_yaml()` as
a context manager. For an edit prepared outside the lock, obtain data and its
revision with `read_episode_yaml_with_revision()` and pass `expected_revision` to
`write_episode_yaml()`. A changed or missing document raises `WorkspaceStoreError`;
reload and reapply the edit. Writing without a revision only creates a new file.
Manual editors do not participate in the workspace lock.

### CMS control responses and Auphonic polling

Editor API responses (including upload acknowledgements and errors) are limited to 8 MiB,
with a 60-second monotonic response-body budget (`DjangoCastClient.response_timeout_seconds`).
Only identity Content-Encoding is accepted. The response cap is independent of uploaded
media size. CMS error diagnostics retain at most 2,000 characters.
Nonempty successful CMS mutation responses must pass strict UTF-8 JSON decoding before
receipt fields are used. Duplicate object members at any depth and nonfinite numbers
(including exponent overflow) fail with a fixed diagnostic. The decoder also caps nesting
at 64 levels, tokens at 100,000 and numeric literals at 256 characters. Check the remote
outcome in Wagtail before retrying a write whose response was refused.

`DjangoCastClient` exposes synchronous methods. Async applications can call them with
`await asyncio.to_thread(client.list_parents)`, for example. Calling a request method
directly on a running event loop raises `CmsApiError` before starting transport work.

Auphonic polling requires finite positive `poll_interval` and `timeout_seconds` values.
One monotonic deadline covers fetches, sleeps and start/rerun grace periods. Fetch timeouts
and the final sleep are capped by the remaining budget; the configured per-request timeout is never increased; late terminal responses are rejected.

Injected Auphonic test transports must support both synchronous and asynchronous requests
(such as `httpx.MockTransport`); CMS test transports must support asynchronous requests.

### Auphonic output download limits and publication

Each output file defaults to a 4 GiB byte limit and a one-hour deadline that covers redirects,
headers and body reads. The whole staged output set has a separate 4 GiB default limit.
Set `PODCAST_PIPELINE_DOWNLOAD_MAX_BYTES` for the per-file limit and
`PODCAST_PIPELINE_DOWNLOAD_TOTAL_MAX_BYTES` for the set limit, both in positive integer bytes.
Set `PODCAST_PIPELINE_DOWNLOAD_TIMEOUT_SECONDS` in finite positive seconds. The client validates
these values before `podcast produce` makes any requests or paid starts. Invalid values also
block `podcast auphonic-cleanup --delete` (listing still works). Library callers can set
`download_max_bytes`, `download_total_max_bytes` and `download_timeout_seconds` on `AuphonicClient`.
The per-file limit and the remaining set budget both apply to declared lengths and streamed bytes.
If either limit is exceeded, staging is discarded and the published set stays intact. Final responses must be HTTP 200,
without a Content-Range header, with identity Content-Encoding and a nonempty body.
Unsolicited partial responses (including HTTP 206), other statuses and empty outputs fail
without replacing the previous output set. Signed download URLs are omitted from download error messages.

All requested outputs download into a private staging directory inside `auphonic/outputs`.
Before downloads start, filenames are planned in response order using Unicode canonical
normalization (NFC) and case folding to detect aliases on all platforms. Original spelling
is retained; collisions get `_2`, `_3`, etc. before the extension, checking each suffix
against the whole plan built so far. For example, `é.mp3` and `é.mp3` become `é.mp3` and
`é_2.mp3`. The plan is revalidated before any existing destination or backup is moved.
Only after the entire set succeeds are affected filenames replaced. A download or publication
failure leaves unrelated files alone. Rollback restores the previous set after handled failures,
including interrupts delivered after a rename takes effect. These guarantees apply to runtime
recovery. Readers should wait for `produce` to finish. `state.json` records downloaded completion only
after publication succeeds. If rollback fails or file identities are uncertain, the error names
the retained staging directory containing recovery files.
Normal success and successfully rolled-back failures remove the owned staging directory.
Cleanup failures log the retained path without masking the publication result or original error.

### Subprocess output and transcription limits

Agent and transcription commands have independent 8 MiB stdout and 1 MiB stderr byte ceilings.
Overflow fails the step and kills/reaps the owned process group, as do timeout and interruption.
Successful agent and transcription commands are retired the same way, so helpers left in their
session/process group do not survive, and uncertain cleanup fails the step;
transcription nonzero-exit diagnostics retain only the final 4096 characters. Agent errors omit raw stderr.
Agent stdin must be completely delivered and fit 1 MiB of UTF-8; protocol stdout is decoded strictly.
Normal stdout is preserved within the limit.
Process supervision needs `os.waitid` with `WNOWAIT`: on macOS that means Python 3.13 or newer
(the repository pins 3.14). Older macOS interpreters refuse agent and transcription calls before launch.
Transcription defaults to a finite 7200-second (two-hour) deadline; `podcast transcribe --timeout`
overrides it with finite positive seconds. This deadline is separate from the agent timeout.
Custom transcribers must write UTF-8, nonblank `transcript.txt` (and optionally `chapters.txt`) into
the supplied `--output-dir` / `{output_dir}`, which is a fresh temporary directory for each invocation.
Only successful fresh output is published; missing, empty or failed custom output keeps the previous transcript.
Custom wrappers must not hard-code the workspace transcript path.

### Dashboard request and history limits

Dashboard JSON requests are streamed with a 64 KiB body ceiling (413 on overflow), regardless of
Content-Length. Invalid UTF-8, malformed JSON, duplicate object members at any depth, nonfinite numbers
(including exponent overflow), or nesting beyond 64 levels returns 400 before work starts;
local host/origin checks run before body consumption. Body acquisition has one monotonic 10-second
deadline across all chunks (408 on expiry); disconnects stop reading with 400. Neither starts work or
changes workspace state, and external request cancellation propagates. A worker launch failure records a failed job,
returns 503 and permits immediate retry while preserving single-flight admission.
Each job retains the latest 128 KiB / 1024 progress entries; oversized writes retain a UTF-8 tail.
JSON/SSE responses include an earlier-progress-truncated notice when history was evicted.
SSE progress IDs increase monotonically and support `Last-Event-ID` reconnects across eviction.
The dashboard retains at most 64 terminal jobs plus all running jobs; evicted job URLs return 404.
Both local UIs admit an explicit Host port only as one to five ASCII decimal digits in 1..65535.
Malformed ports return the existing 403 Forbidden host response before reading a body, including
oversized numbers, Unicode digits and out-of-range values. IPv4/IPv6 loopback hosts remain supported.
Workspace reads/writes, job admission and context-lock acquisition run in Starlette's worker pool.
Snapshots are serialized or copied under that same lock; HTTP/SSE sends and waits happen after it is
released. A slow workspace operation or a worker-held lock can delay dependent snapshots, while the
event loop and Done/shutdown endpoint remain responsive. Initialization and ingest use this boundary too.

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

### CMS recovery checkpoints

CMS recovery state is validated before mutation: IDs must be positive integers or null and present flags must
be booleans. `cms/draft.json` now records the resolved `parent_id` as well as the site. Changing podcasts requires
an explicit reset after checking the old episode and Audio in Wagtail; no old IDs are reused. Legacy state with
an episode ID is adopted only after verifying its parent. For legacy upload-only state, verify the Audio's
intended podcast and add its `parent_id`, or explicitly reset the state to start fresh. Valid partial upload
checkpoints remain resumable. State for a different site remains ignored.

If a remote upload or episode write succeeds but its local checkpoint fails, the error gives the confirmed
object ID and the fields to merge into `cms/draft.json`. Repair write access and those fields before rerunning,
preserving the previous state and its other fields. The command stops immediately without retrying the remote
write. Malformed successful API identities also stop the run; check Wagtail for the remote outcome before retrying.

To reset deliberately, move `cms/draft.json` to a backup after recording any unattached Audio ID for manual
cleanup/recovery. Reset abandons automatic reuse of those IDs. Alternatively clear both IDs to null when
abandoning them; a stale parent with no IDs is harmless. A missing stored episode under a matching recorded
parent falls back to slug lookup; a moved episode or unverifiable legacy episode requires the explicit reset.


### CMS preview and publication boundaries

`cms-draft --dry-run` includes an `intent` object: normal reuse-or-upload, forced upload with `--new-audio`,
and an explicit transcript request with `--transcript`. Local recovery IDs and pending transcripts are shown
when stored `parent_id` matches an explicit `cms.parent_id`. With `parent_title` or automatic parent resolution,
local IDs remain unresolved in the preview. Legacy upload-only state requires a verified parent binding or the
backup-first reset before a live run; the preview does not verify that binding. Parent verification, remote
draft/audio lookup and endpoint capabilities remain unresolved;
the payloads are conditional request templates. Dry runs do not look up tokens, use the network, take ownership
or write files.

Implicit CMS audio uses a single filename component for `auphonic.output_basename` or `episode_id`, matching
Auphonic's directory-separator rules. The preferred MP3, single-MP3 fallback and additional formats must resolve
inside `auphonic/outputs`, including symlinks. Use episode-local `cms.audio_file` explicitly for a file elsewhere.

Live drafting reads the selected copy and configuration under CMS ownership and rechecks the complete selected
text/format and effective configuration immediately before episode create/update requests, including contributor
updates. If inputs change during upload/setup, the command stops with a retry message. An uploaded Audio remains
checkpointed for the retry; check the new selection/configuration before rerunning. This check does not lock out
manual edits after the final comparison or roll back an episode request that already succeeded.

API errors show the trusted method, endpoint, HTTP status and allowlisted error/field categories. Redirects show
only the destination origin (no userinfo, path, query or fragment) and are never followed. Response bodies and raw
server messages are omitted, including revision-conflict payloads; conflicts still explain checking Wagtail before
retrying. Checkpoint recovery instructions omit remote edit URLs too.

## Standalone picker limits

`podcast pick --web` accepts at most eight HTTP handlers at once; additional connections receive 503.
Header and body reads share a ten-second absolute deadline, including clients that send bytes slowly.
Mutation bodies require exactly one unsigned decimal `Content-Length`, at most 64 KiB; transfer encoding,
duplicate lengths and incomplete bodies are refused and the connection closes. Admission precedes JSON decoding.
JSON must be a UTF-8 object with unique members, at most 64 container levels, integers of at most 256 characters,
finite constants and encodable strings. Invalid objects receive a payload-free 400; oversized bodies receive 413.

Each assets response reads current state, candidate membership and selection artifacts under the workspace
publication lock. `--asset` continues to restrict the picker to that asset. Browser drafts remain local.
The picker refuses Done with 409 while a refinement is pending and rejects mutations after accepting Done.
The thirty-minute server timeout stops admission, then drains accepted handlers and their agent publication;
agent execution retains its configured finite timeout (default fifteen minutes). It does not cancel paid work
just because the browser closes or times out. Startup failures close the bound socket and cancel/join the timer.
Refinement failures return `refinement_failed` and a local reference; stderr records the same reference and
exception type, without prompt content, agent output, paths, credentials or raw exception text.

### Tag example file limits

`tags.examples` requires a coherent regular UTF-8 file (no symlinks/FIFOs): at most 8 MiB,
256 KiB per line and 10,000 physical lines including blanks. Duplicate JSON members,
nonstandard/nonfinite numbers, excessive integers and nesting beyond 64 containers are refused
with a line/category diagnostic before the agent starts. See `few-shot-dataset-jsonl.md`.

### Agent transport completion

Agent calls require complete UTF-8 stdin delivery and retire the owned process group before reaping the leader, including successful calls.
A wrapper must keep all helpers in its session/process group; detached sessions are outside this ownership boundary.

Agent cleanup adds at most a one-second leader reap allowance and three 50 ms thread joins.
All pipe closes are attempted even after a signal/wait failure. An uncertain cleanup refuses success;
a primary timeout/interruption remains primary with a fixed cleanup note. Check owned processes before retrying.
Group signaling requires an unreaped child identity; external reaping revokes this authority.
On Darwin, an EPERM group signal is accepted only when waitid confirms exit and libproc proves
the reserved leader is the sole member.
Agent stdout must be exact UTF-8; stderr remains a separately byte-bounded diagnostic stream.
Transcriber stdout is not protocol output: it is still byte-bounded but decoded with replacement and ignored.

Agent protocol stdout must contain exactly one JSON object, optionally wrapped in a complete
```` ```json ```` code block with no surrounding prose. Multiple results, duplicate members at any depth,
nonstandard constants, nonfinite floats and surrogate strings are refused. Before JSON hydration,
control input is capped at 8 MiB UTF-8, 64 container levels, 100,000 tokens (container starts,
member names and scalar values), and 256 characters per numeric scalar. Admission is a single linear
scan; an unterminated string is refused immediately. Invalid/excessive results
raise a content-free agent error before candidate or review publication; fix the wrapper and retry.

Complete runner stdin, whether inline or from a prompt file, is limited to 1 MiB UTF-8 before launch.
Prompt files must be regular, coherent, non-symlink files; FIFOs, oversized/growing files and observed
generation changes are refused before any agent starts. Inline text is the explicit alternative.
Agent failures show fixed categories, exit status and retry guidance; raw stderr and JSON decoder
details are omitted from ordinary errors and default chained tracebacks. Check configuration before
retrying a failed agent. Prompt/result prose remains private even when it contains terminal controls.

### Deterministic draft inputs

`draft-candidates` reads a regular coherent workspace summary of at most 1 MiB and chapters of
at most 1 MiB / 200 physical lines. Fake `draft-demo` uses the same chapter envelope and an
8 MiB transcript envelope. These readers reject special files, invalid UTF-8 and observed generation
changes before candidate/protocol publication. Episode metadata references and fixed defaults must
be relative paths inside the workspace with no `..` or symlink components. Missing optional chapters
or demo inputs stay empty; invalid existing inputs fail. `draft-candidates --chapters` remains an
explicit operator file option outside the workspace, with the same regular-file and byte/line checks.

Summary JSON for `draft-candidates` uses the strict control JSON contract above before EpisodeSummary
validation: duplicate members, NaN/Infinity, nonfinite numbers and excessive structure fail with zero
candidate writes. The summary file itself retains the tighter 1 MiB ceiling.
`draft-candidates --candidates N` supports **1–10** variants per asset (default 3). The direct
`generate_draft_candidates` API checks the same integer range, rejecting booleans and invalid config
values before any rendering/storage work. At admitted counts the variant IDs and content remain deterministic.

### RSS HTML comments

RSS example HTML removes complete `<!-- ... -->` comments with a forward-only
scan. Repeated openers in an unterminated comment do not trigger repeated suffix
searches; an unterminated final comment discards the rest of that HTML value.
Text outside comments is retained and XML/feed limits are unchanged.

### RSS description extraction

Descriptions may contain plain HTML text/CDATA or mixed XML content. Mixed-content
serialization retains the leading text, child order and each child's trailing text
exactly once, including namespaced children.

Content tag preference takes precedence over document order: `encoded` before
`description`, `summary` before `subtitle`, and `pubDate`, `published`, `updated`,
then `date`. Empty values fall back; repeated elements of the same tag retain
document order. Namespaces continue to match by local name.

XML decoding happens once. Childless RSS content is interpreted as the HTML string
carried by XML escaping or CDATA; mixed-content XML escapes its leading text just
like its child text/tails. HTML normalization retains named/numeric entities in
text and attributes (`&lt;script&gt;` remains literal text, `&amp;` remains an entity).
It normalizes whitespace without a second global entity decode. Existing example
exports with decoded markup should be regenerated from the feed.

CMS example descriptions and shownotes use this same HTML normalizer and retain
entity syntax too; `&nbsp;` stays encoded instead of being replaced with a regular
space. Regenerate older CMS exports to restore any previously decoded literal markup.
