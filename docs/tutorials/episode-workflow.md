# Episode workflow: init → ingest → draft → review → pick → produce → cms-draft

This guide ties together the MVP pipeline steps for a single episode and shows where each step writes in the
workspace.

## Ingest tracks

Ingest refuses a glob that matches no files and reports the resolved media directory and glob.
The existing `episode.yaml` remains byte-for-byte unchanged; clearing tracks requires an explicit edit.

## 1. Initialize a workspace (init)

```bash
podcast init --episode-id ep_001 --workspace ./workspaces/ep_001
```

Notes:

- `--workspace` must not exist; the command creates it.
- The workspace is assembled in a hidden sibling directory (`.podcast-init-*`) and moved into place in one step,
  so a failed or interrupted initialization leaves nothing at the workspace path and the command can be retried right away;
  parent directories remain. Init never deletes anything on failure: an existing workspace, or content that
  appears at the workspace path while init runs, is preserved and reported, and the partial staging directory
  is left in place. The error includes the failure cause and the staging path; inspect that directory and
  remove it yourself once you no longer need it.
- These guarantees cover failures and interruptions of init itself. Another process running as the same user
  with write access to the parent directory that replaces the workspace or staging directory while init runs
  is outside the threat model: such a process can delete or overwrite the workspace directly.
- If you omit `--workspace`, the default is `./episodes/<episode_id>`.

## 2. Export and transcribe audio

Export two things from Ultraschall/Reaper once the edit is done:

- **Per-speaker stems for Auphonic** (required): one FLAC per speaker, synced, all the same length and trimmed and
  edited as in the project (in Reaper: render the stems of the selected tracks over the whole project, for example into
  a `stems/` folder next to the project). Auphonic gets these individual tracks and does the mixing itself; see
  [6. Produce with Auphonic](#6-produce-with-auphonic-produce). The raw recordings in the Reaper media folder are not a
  substitute unless they already are synced, trimmed and edited.
- **A master mix for transcription** (optional, for example an MP3): `podcast transcribe` needs a single audio file.
  A master mix is only an input for transcription, never for Auphonic.

`podcast transcribe` reads `inputs.transcription_audio` from `episode.yaml`; it never reads the Auphonic settings. The
recommended transcription path is `podcast-transcript` with the `voxhelm` backend.

```bash
# Example episode.yaml snippet (merge into an existing inputs: section if there is one)
cat >> ./workspaces/ep_068/episode.yaml <<'YAML'
inputs:
  transcription_audio: /Users/jochen/Documents/REAPER Media/pp_068/pp_068_mix.mp3
YAML

# Transcribe through podcast-pipeline using podcast-transcript + Voxhelm
VOXHELM_API_BASE=https://voxhelm.home.xn--wersdrfer-47a.de \
VOXHELM_API_KEY=your_voxhelm_token_here \
podcast transcribe \
  --workspace ./workspaces/ep_068 \
  --command transcribe \
  --arg=--backend \
  --arg=voxhelm
```

Notes:

- Voxhelm requires a reachable service instance and a valid API key.
- `podcast transcribe` imports the generated plain-text transcript into `transcript/<mode>/transcript.txt` inside the workspace.
- `podcast-transcript` keeps its artifacts (audio copy, chunks, chunk transcripts) in an episode-local cache under
  `transcript/<mode>/.podcast-transcript/<audio-hash>/`, so episodes with the same audio file name never share a
  transcript. Re-running on unchanged audio reuses that cache; see the transcription notes in the episode.yaml reference.
- Without `inputs.transcription_audio`, `podcast transcribe` uses a single track with role `mix`/`master`/`final`/
  `mixdown`, or the only track. With several stems and no mix track it asks you to set `inputs.transcription_audio`.
  Older workspaces that set `auphonic.input_file` for transcription must move that path to
  `inputs.transcription_audio`.
- If you want to override the backend or pass extra transcription flags, add more `--arg` entries; for example
  `--arg=--language --arg=de`.

## 3. Draft text assets (draft)

`podcast draft` runs the transcript chunking + summary + candidate generation pipeline. It reuses an existing workspace
if one is present. Supplying `--transcript` again replaces the workspace transcript and clears the chunks and summaries
built from the previous one.

```bash
podcast draft \
  --workspace ./workspaces/ep_068 \
  --episode-id ep_068 \
  --host Jochen --host Dominik \
  --candidates 3
```

The `--host` flag is repeatable and persists host names to `episode.yaml`. On subsequent runs without `--host`, the
stored names are reused automatically. Host names are injected into all LLM prompts (summarization and candidate
generation) to prevent hallucinated speaker names. The dashboard's Summarize action also uses the stored hosts
in both the chunk and episode summary prompts.

When the workspace already has `transcript/transcript.txt` from `podcast transcribe`, `podcast draft` reuses it. Pass
`--transcript /path/to/file.txt` only when creating a new workspace from an external transcript or when you want to
replace the transcript currently stored in the workspace.

Outputs:

- `transcript/` contains the ingested transcript + chunk files.
- `summaries/` contains chunk summaries and the episode summary. Before publishing a completed episode summary,
  the pipeline checks the serialized UTF-8 sizes of the JSON (1 MiB), Markdown (32 MiB), and HTML (64 MiB)
  artifacts. If an artifact exceeds its limit, the error names the file and the previous completed summary
  stays available.
- `copy/candidates/<asset_id>/` contains candidate JSON + Markdown + HTML files.

Tag assets (`cms_tags`, `audio_tags`, `itunes_keywords`) aim for 6-10 specific terms. Generated tag candidates are
filtered deterministically: host names, show-level constants such as `Python Podcast` or `Deutsch`, prose-like entries
and anything beyond the cap (12 by default) are dropped. To teach the drafter your published tag style, export past
episodes once and reference them from the global config:

```bash
podcast rss-examples \
  --feed-url https://example.com/feed.xml \
  --output ~/.config/podcast-pipeline/rss_examples.jsonl
```

```yaml
# ~/.config/podcast-pipeline/config.yaml
tags:
  examples: rss_examples.jsonl
  exclude: [Programmierung, Softwareentwicklung]
```

See `reference/configuration.md` ("Tag assets") for all options.

### Replacing the transcript

- The new transcript is copied completely into a staging file next to `transcript/transcript.txt` before anything else
  changes. A missing or unreadable source, a failed copy or an invalid `episode.yaml` leaves the transcript, chunks and
  summaries untouched.
- Chunks and summaries are then removed, and only after that the staged transcript replaces the old one. Derived
  artifacts therefore never sit next to a transcript they were not built from. If the command is interrupted in
  between, the previous transcript is still in place without chunks and summaries; rerun `podcast draft` (with the new
  `--transcript` to retry the replacement) and they are rebuilt.
- Passing the workspace's own `transcript/transcript.txt` (or a hard link to it) as `--transcript` changes nothing;
  existing chunks and summaries are reused.

## 4. Run the review loop (review)

```bash
podcast review \
  --workspace ./workspaces/ep_001 \
  --episode-id ep_001 \
  --asset-id description \
  --max-iterations 3
```

Notes:

- Add `--fake-runner` to use the built-in stub creator/reviewer.
- Review iterations are written under `copy/reviews/<asset_id>/` and protocol state under `copy/protocol/<asset_id>/`.
- Each completed round checkpoints before the next agent call. Repeating review with the same maximum
  resumes missing rounds; terminal decisions make no agent calls and keep later producer selections.
- A reviewer request for `needs_human` stops immediately. On convergence, the selected draft is written
  to `copy/selected/<asset_id>.*` only if the reviewed selection is still current. A newer pick or custom
  edit is retained and reported as a selection conflict. See [review-loop recovery](../how-to/review-loop.md).

## 5. Pick final copy (pick)

```bash
# Web UI (recommended) — opens a browser for full-text side-by-side comparison
podcast pick --workspace ./workspaces/ep_001 --web

# CLI — interactive prompt with truncated previews
podcast pick --workspace ./workspaces/ep_001
```

Notes:

- `--web` opens a local web UI for full-text comparison of all candidates per asset. Select candidates by clicking, then
  press "Done" to shut down the server.
- Tag assets (`cms_tags`, `audio_tags`, `itunes_keywords`) are not picked as whole candidates in `--web` or the
  dashboard. All their candidates (including regenerated ones) are merged into one deduplicated suggestion pool, ordered
  by how many candidates proposed each tag (`×3` means three candidates agreed), after the tag filter (see "Draft text
  assets"). Click a tag to include or exclude it, add your own tags, or use "Use top N" to take the most agreed ones.
  Each change writes `copy/selected/<asset_id>.md` right away (changes are saved one at a time, and "Done"/"Close"
  waits for pending saves); at most `tags.max` entries (default 12, Apple's
  historical iTunes keyword limit) can be selected. In `--web`, tag assets never block "Done": an asset without
  selected tags is simply left empty. The dashboard keeps the raw candidates in a collapsed "Raw candidates" section, where
  they can be deleted but not selected as a whole. Selecting a whole tag candidate through the API is rejected when it
  has more than `tags.max` entries.
- Without `--web`, the CLI prompts when multiple candidates exist and writes the selection to `copy/selected/`.
- Use `--asset-id` and `--candidate-id` to pick a specific candidate non-interactively (CLI only).
- A selection has one canonical file: `.md` (with a derived `.html`), `.txt` or `.html`. Selecting a candidate in
  another format removes the files of the previous format after the new ones are written, so `copy/selected/` never
  keeps a stale `.html` or `.md` next to a newer selection. If writing fails, the previous canonical file is unchanged;
  if only the final removal fails, the command reports it and repeating the selection finishes the switch.

### Write or edit the selected text

When no candidate is quite right (for example you want the slug `python-315` and none of the candidates offers it),
write the selection yourself. Both `podcast pick --web` and the dashboard's Pick tab show a "Custom text" card below
the candidates of each asset except the tag assets, which use the tag pool editor instead (see above). Custom text
for a tag asset is rejected by both endpoints, so the `tags.max` cap cannot be bypassed:

1. Press "Edit" on a candidate to copy its text into the editor, or type from scratch.
2. Change the text and press "Use custom text" (or "Save" once a custom text is the current selection).

Notes:

- The text is stored in the asset's natural format: plain text (`copy/selected/<asset_id>.txt`) for `slug`,
  `title_detail`, `title_seo`, `subtitle_auphonic` and `summary_short`; Markdown (`.md` plus the derived `.html`) for
  everything else, such as `description` and `shownotes`. "Edit" drops Markdown headings from a candidate when the
  asset is stored as plain text.
- `slug`, the titles and `subtitle_auphonic` must be a single line. Slugs must be lowercase letters, digits and single
  hyphens (`python-315`, `python-3-15`); an invalid slug is rejected with a suggested fix. Empty text is rejected.
- A custom selection counts as selected: it gets a check mark, counts toward "Done" in `--web`, and `podcast status`
  and `podcast produce` read it like a picked candidate.
- In `state.json` the asset keeps its candidates but has no `selected_candidate_id`; a `manual_selection` provenance
  entry (`ref` = asset id) marks the manual edit, and `copy/provenance/manual_selection/<asset_id>.json` records the
  format, the candidate the text started from (if any) and whether it was changed. Selecting a candidate again (in a UI,
  with `podcast pick`, or by a converged review loop) replaces the custom text and drops that marker.
- Locked assets (`slug`, titles, `subtitle_auphonic`) stay locked for the review loop: a reviewed candidate that
  differs from the custom text is flagged as `locked_selection`.
- The endpoints are `POST /api/select-custom` (`{"asset_id", "text", "based_on_candidate_id"?}`) for `--web` and
  `PUT /api/assets/<asset_id>/selection` (`{"text", "based_on_candidate_id"?}`) for the dashboard; both follow the
  local web UI rules below.

### Refine a text with an instruction

Typing every change by hand is tedious; you can also tell the drafter agent what to change. Below the custom-text
editor, choose the text to refine (the editor's text or one of the candidates), type an instruction such as `kürzer`,
`ohne Emojis` or `Slug nur python-315`, and press "Refine" (or Enter).

- The drafter (the same agent CLI and timeout as `podcast draft`) rewrites only what the instruction asks for. Its
  prompt contains the asset's guidance, the text, the instruction, the asset's editorial notes (dashboard "Editorial
  Notes") and, when it exists, the episode summary as context.
- The complete prompt, including guidance and schema, is limited to 256 KiB of UTF-8 bytes. Oversized requests
  fail before the agent runs, and both the dashboard and the pick UI show the limit message; shorten the source,
  notes or summary and retry. No factual context is truncated,
  and existing candidates and selections survive the refusal.
- Optional summary JSON must be a regular, nonsymlink UTF-8 file of at most 1 MiB. Refinement omits missing
  summaries; unreadable, oversized, malformed or detectably changed files are omitted with a short diagnostic.
  Rebuild the summary to restore context. The reader checks the opened file before and after its bounded read;
  this detects changes rather than locking out manual editors.
- The result is a new candidate, labelled `refined: <instruction>`, stored in the asset's natural format under
  `copy/candidates/<asset_id>/`. Its provenance (`kind: llm_refine`) records the instruction and the candidate it was
  based on. Select it, press "Edit" to tweak it by hand, or refine it again.
- Nothing is selected automatically. Answers that are empty, or an invalid slug for `slug`, fail with an error instead
  of creating a candidate.
- In `--web` the request runs while the page stays usable; in the dashboard it runs as a `refine:<asset_id>` job whose
  log appears below the candidates. One refinement per asset runs at a time (`409` otherwise).
- Unlike "Generate More" (dashboard), which drafts new candidates from the episode summary, refining keeps the chosen
  text and only applies the instruction.
- Endpoints: `POST /api/refine` (`{"asset_id", "instruction", "candidate_id"? | "text"?}`) for `--web`, answering
  with the new candidate, and `POST /api/assets/<asset_id>/refine` (`{"instruction", "candidate_id"? | "text"?}`) for
  the dashboard, answering with a `job_id`. Without `candidate_id` and `text`, the current selection is refined.

## 6. Produce with Auphonic (produce)

`podcast produce` builds the Auphonic payload from `episode.yaml`, the global config and the selected copy, starts an
Auphonic **multitrack** production from the per-speaker stems, waits for it and downloads the outputs to
`auphonic/outputs/`. Auphonic mixes the stems itself; never give it a master mix. List the stems you exported in
step 2 and pick a multitrack preset:

```yaml
auphonic:
  preset: python_podcast_multitrack   # key in auphonic.presets of the global config, or a preset UUID
  tracks:
    - id: jochen
      path: /Users/jochen/Documents/REAPER Media/pp_072/stems/01-jochen.flac
    - id: dominik
      path: /Users/jochen/Documents/REAPER Media/pp_072/stems/02-dominik.flac
    - id: johannes
      path: /Users/jochen/Documents/REAPER Media/pp_072/stems/03-johannes.flac
      algorithms:          # optional Auphonic per-track algorithms
        denoise: true
```

Without `auphonic.tracks`, the ingested `tracks` are the stems (tracks with a mix/master/final/mixdown role are
skipped), so ingesting the stems folder works too. A production costs Auphonic credits, so preview the payload first:

```bash
podcast produce --workspace ./workspaces/ep_001 --dry-run   # print the payload, no API call
podcast produce --workspace ./workspaces/ep_001             # start, wait, download
podcast produce --workspace ./workspaces/ep_001 --restart   # discard the stored production, start a new one
podcast produce --workspace ./workspaces/ep_001 --update --dry-run   # print the update requests, no API call
podcast produce --workspace ./workspaces/ep_001 --update    # edit the stored production, start it again, download
podcast auphonic-cleanup --workspace ./workspaces/ep_001    # list superseded productions, no API call
podcast auphonic-cleanup --workspace ./workspaces/ep_001 --delete   # delete them from the Auphonic account
```

Notes:

- Credentials: `AUPHONIC_API_KEY` (an API key from your Auphonic account settings, sent as a Bearer token), or
  `AUPHONIC_USER` and `AUPHONIC_PASSWORD` (HTTP Basic). The API key wins when both are set. `AUPHONIC_BASE_URL`
  overrides the API base URL; it must be an `https://` URL with a host and valid port and without credentials, `?` or `#`
  (otherwise `produce` stops before any request), so the key or password never travels in cleartext. Credentials are only sent to that URL's origin, never to output download hosts on
  other origins. Crash reports of the `podcast` CLI do not list local variables, so an unexpected error does not
  print the Auphonic key or password, or the CMS API token, to the terminal.
- The preset must be an Auphonic multitrack preset (created in Auphonic's multitrack mode). Single-track presets, such
  as a mono preset built for one pre-mixed file, do not apply to multitrack productions. Before creating a production,
  `produce` reads the preset and stops on a type mismatch, so nothing is created or paid for.
- The multitrack preset must not contain speaker tracks: Auphonic appends the preset's tracks to the production's, which
  duplicates them ("Multiple tracks with ID ... found"). `produce` stops before creating a production when the preset
  has any; put per-track algorithms into `auphonic.tracks[].algorithms` instead.
- The preset check fails closed: preset data that is not a mapping, an `is_multitrack` other than `true`/`false`
  (a missing field means a legacy single-track preset) or unreadable `multi_input_files` stop `produce` and
  `--update` before anything is created or changed. Auphonic API responses are capped separately at 8 MiB on
  the wire and 8 MiB after decoding. Duplicate JSON object members stop the operation. See "Auphonic control
  response limits" in `reference/configuration.md`.
- `--dry-run` prints the full payload, including `is_multitrack: true`, the `multi_input_files` track list with
  each stem's id and resolved path, and `output_basename`, without loading credentials or calling the API.
- Output files are named after the episode: `output_basename` defaults to `episode_id` (for example `pp_72.mp3`,
  `pp_72.m4a`, `pp_72.opus`); set `auphonic.output_basename` to override it. Without it Auphonic would name the
  outputs after the first track.
- Each stem is a local path (relative to the workspace is ok) or an `http(s)://` URL. `produce` follows Auphonic's
  [multitrack API](https://auphonic.com/help/api/multitrack.html): create the production with `is_multitrack: true`
  and one `multi_input_files` entry `{type: "multitrack", id: <track id>}` per stem (URL stems carry their
  `input_file` in this request), upload all local stems in one multipart request to `production/{uuid}/upload.json`
  with the track id as each file's form field name, then call `production/{uuid}/start.json`.
- `auphonic.input_file` is rejected in a multitrack production. A single-file production is only available as an
  explicit opt-in: `auphonic.mode: single_track` with `auphonic.input_file` and a single-track preset.
- The production's UUID is stored in `state.json` as soon as it is created, before the start request, so a start
  whose outcome is unknown (for example a timeout) is never paid for twice. While it runs, `produce` polls it every
  15 seconds for up to an hour. Status `3` (Done) downloads the outputs. Status `2` (Error), `9` (Incomplete),
  `11` (Outdated) and `98` (Empty Production) fail, as do `10` (Not Started Yet; tolerated for two minutes right
  after `produce` started the production) and `15` (Production Changed, edited in Auphonic after it finished). Every
  other status (upload, waiting, audio processing/encoding, file transfers, speech recognition, stopping, and unknown
  codes) counts as still running. The numeric status code decides; `status_string` is only used when the code is
  missing. A numeric status must be an integer from 0 to 999 or a string of one to three ASCII digits; anything else
  numeric-looking (out of range, signs, decimals, exponents, non-ASCII digits, floats, booleans) fails the poll with a safe API error and changes nothing,
  and an unknown code stays running even when its `status_string` says Done.
- A rerun (for example after a timeout or a crash) resumes the stored production instead of starting a second, paid
  one. When the stored production has failed or was never started (for example the upload failed), rerunning reports
  the same status; fix the cause, then use `--update` (same stems) or `--restart` (new production) on purpose. The
  dashboard's produce button always resumes; use the CLI for `--update` and `--restart`.
- A stored production of the other type is never resumed: when `state.json` still holds a single-track production (for
  example a pre-mixed one from before productions became multitrack) and `episode.yaml` now configures stems, `produce`
  stops and asks for `--restart`.

### Re-run a production without uploading the stems again (--update)

When the production itself is fine but its settings are not (the preset lacked an output format such as Ogg Vorbis or
the audiogram video, metadata such as `year` was missing, chapters changed), `--update` edits the production stored in
`state.json` and starts it again instead of creating a new one. The stems stay in Auphonic, so nothing is uploaded
(a 70-minute three-speaker episode is about 750 MB of FLAC).

- Re-running a finished production costs Auphonic credits again, like a new production. `produce` says so before it
  starts.
- `produce --update` rebuilds the payload from `episode.yaml`, the global config and the selected copy as usual and
  sends everything except the inputs as one update to `production/{uuid}.json`: `preset`, `title`,
  `output_basename`, `metadata` and `chapters`. Sending the preset makes Auphonic re-apply it, so the production gets
  the preset's current output files (replacing the old list, not appending to it) and loses its old chapters before the
  payload's chapters are set. Then each track's `offset` (0 when unset) and `algorithms` go to
  `production/{uuid}/multi_input_files/{id}.json`. The UUID in `state.json` does not change.
- Before the paid start request `produce` checks the updated production: it must have exactly the payload's chapters
  and at least one output file, otherwise it stops without starting it.
- `--update` refuses, before changing anything, when there is no stored production, when it is still running (a plain
  rerun follows it), when Auphonic does not allow changes, and when the inputs differ: another production type, other
  track ids, or a stem whose file name differs from the uploaded one. Changed stems need `--restart`, which uploads
  them. A stem re-exported under the same file name is not detected; use `--restart` after re-exporting.
- Auphonic merges per-track algorithms: an algorithm removed from `auphonic.tracks[].algorithms` keeps its previous
  value in the production. Metadata fields the preset leaves empty also keep their previous value when they are
  removed from `episode.yaml`. Use `--restart`, or edit the production in Auphonic, to drop them.
- After the start, the production's previous `Done` (or `Production Changed`) status is ignored for up to two minutes
  until Auphonic reports it running again, so the previous run's outputs are never downloaded as the new ones. The new
  outputs overwrite files of the same name in `auphonic/outputs/`; files of an older run with other names stay.
- `--update` never uses Auphonic's `reset_data` flag: on a multitrack production it deletes all tracks with their
  uploaded stems. `multi_input_files` is never part of the update either, since Auphonic would add the tracks a second
  time.
- Deleting the current production in the Auphonic web UI deletes its uploaded stems too, so it cannot be resumed and
  `--update` no longer works for it. When Auphonic answers 404 for the production stored in `state.json`, a plain
  `produce` and `produce --update` stop before creating, updating or starting anything, record it as `missing` in the
  history and ask for `podcast produce --restart`, which uploads the stems to a new (paid) production. The same
  happens when the production is deleted while `produce` polls it.
- `--update` and `--restart` cannot be combined. `--update --dry-run` prints `production_uuid`, `production_update`
  and `track_updates` without calling the API (it still needs a stored production UUID).

### Clean up superseded productions (auphonic-cleanup)

`--restart` creates a new production and leaves the previous one (never started, failed, or a finished one that was
redone) in the Auphonic account. `state.json` therefore records every production `produce` created for the workspace
in `auphonic_productions`, not only the current one in `auphonic_production_uuid`. Each entry has the production's
`uuid`, its `status` in the history (`current`, `superseded` or `deleted`), `created_at`, `last_known_state` (the
Auphonic state last seen: `done`, `error`, `not_started`, `changed`, `running` or `missing`), `outputs_downloaded_at`
and `output_receipt` (workspace-relative paths and byte lengths of the downloaded files), and for a replaced one `superseded_at`, `superseded_by` and `superseded_reason` (for example
"replaced by `podcast produce --restart`"). A production UUID stored before this history existed is added to it the
next time `produce` replaces it (without `created_at`).

Deleting a production cannot be undone, so cleanup is explicit:

- `podcast auphonic-cleanup` (or `--dry-run`, the default) lists each production that would be deleted, by UUID with
  its reason, without loading credentials or calling the API.
- `podcast auphonic-cleanup --delete` deletes them with `DELETE production/{uuid}.json` and marks them `deleted` in the
  history.
- `podcast produce --cleanup` does the same right after the production finished and its outputs were downloaded. Set
  `auphonic.cleanup_superseded: true` in `episode.yaml` or the global config to make this the default; `--no-cleanup`
  turns it off for one run. In this automatic mode a refused or failed deletion is a warning: the production itself
  succeeded, and `podcast auphonic-cleanup --delete` retries later.

What is deleted, and when:

- Only `superseded` entries of the workspace's own history. The current production, productions already marked
  `deleted`, and anything that is not in the history (for example productions of other episodes, or ones created in
  the Auphonic web UI) are never deleted.
- Before deleting, cleanup confirms with Auphonic that the current production is still Done and checks that every
  file in its latest output receipt is a regular file at the recorded workspace path with the recorded byte length
  (`auphonic/` or `auphonic/outputs/` may be a symlink, for example to an external media disk).
  Run `podcast produce` again to refresh the current outputs if a legacy workspace lacks a receipt or a file no longer
  matches. An `--update` clears the receipt until the new run's outputs have been downloaded.
- A superseded production that Auphonic reports as still running is skipped; one that no longer exists (404 on the
  status check or on the delete request, for example because it was deleted in the Auphonic web UI) is recorded as
  deleted. Failed deletions stay `superseded` and are retried by the next cleanup.
- If a production is gone from Auphonic but the workspace write fails, cleanup reports its UUID as pending local
  settlement and stops with the outcomes already recorded. Fix the write error and run
  `podcast auphonic-cleanup --delete` again. A 404 on the status check records the deletion locally without sending
  another DELETE.
- When the current production itself was deleted in Auphonic, the cleanup deletes nothing, records it as `missing` and
  asks for `podcast produce --restart`; after the new production finished, the old one is recorded as deleted.
- `auphonic-cleanup --delete` holds the same `auphonic/.produce.lock` as `produce`, so it never runs next to one.

Output downloads have a 4 GiB default limit per file and another 4 GiB limit for the whole staged set.
Set `PODCAST_PIPELINE_DOWNLOAD_TOTAL_MAX_BYTES` to change the set limit. Interrupted or oversized
downloads remove partial temporary files and preserve the existing output set.
Publication rolls back even if a rename took effect before the interrupt. If rollback fails
or the file identities are uncertain, keep the staging directory named in the error and
inspect its recovery files before retrying.

## 7. Create the episode draft on the website (cms-draft)

`podcast cms-draft` creates (or updates) the episode page as an unpublished **draft** on a
[django-cast](https://github.com/ephes/django-cast) site such as python-podcast.de, through django-cast's editor API.
It replaces the manual steps of uploading the MP3, creating the episode and pasting the copy into Wagtail. It never
publishes; `podcast cms-publish` does that after a confirmation.

Configure the site once in the global config (`~/.config/podcast-pipeline/config.yaml`; see
`reference/configuration.md` for every key) and put the API token into the environment:

```yaml
cms:
  base_url: https://python-podcast.de/
  token_env: PYTHON_PODCAST_API_TOKEN
  parent_id: 120              # the podcast page; optional when the token sees exactly one podcast
  audio_collection: 1
  detail_intro_html: >-
    <h2>Shownotes</h2><p>Unsere E-Mail für Fragen, Anregungen &amp; Kommentare:
    <a href="mailto:hallo@python-podcast.de">hallo@python-podcast.de</a></p>
  host_links:
    Jochen: https://fedi.wersdoerfer.de/@jochen
    Dominik: https://mastodon.social/@oryon_dominik
    Johannes: https://mastodon.world/@shezi
  contributors:               # host name -> django-cast Contributor slug (role host), or {slug, role}
    Jochen: jochen
    Dominik: dominik
    Johannes: johannes
```

```bash
export PYTHON_PODCAST_API_TOKEN=...                      # DRF token of a user with Wagtail admin access
podcast cms-draft --workspace ./workspaces/pp_73 --dry-run   # print the upload and episode payloads, no API call
podcast cms-draft --workspace ./workspaces/pp_73             # upload the MP3, create or update the draft
podcast cms-publish --workspace ./workspaces/pp_73           # publish after checking the preview (asks first)
```

What `cms-draft` sends:

- **Audio**: one `POST api/editor/media/audios/` with the Auphonic MP3 (`auphonic/outputs/<output_basename>.mp3`,
  `output_basename` defaulting to `episode_id`; set `cms.audio_file` in `episode.yaml` to upload another file), the
  episode title as `title`, `subtitle_auphonic` as `subtitle`, `auphonic.chapters` (or the chapters in `state.json`)
  as `chaptermarks` lines `HH:MM:SS Title`, `cms.audio_collection` as `collection`, and
  `transcript_diarization_mode` (`cms.transcript_diarization`, default `enabled`) when the site accepts it.
  django-cast accepts exactly one file per upload.
- **Episode**: `POST api/editor/episodes/` with the podcast page as `parent`, `title` (`title_detail`), `slug`,
  `seo_title` (`title_seo`), `search_description` (`summary_short`), `tags` (`cms_tags`), `keywords` (the
  `itunes_keywords`, comma-separated), `explicit` (`cms.explicit`, default 2 = no) and `podcast_audio`. The overview is
  the selected description as HTML without its leading title heading, followed by an `audio` block for the uploaded
  Audio: without that block no player renders on the page. Hosts of the episode (`hosts` in `episode.yaml`) are linked
  to their `cms.host_links` profile at their first unlinked mention; links already in the selected description (host
  links, inline topic links) are kept as they are. The detail section is `cms.detail_intro_html` followed by the
  selected shownotes.
- **Contributors**: the episode's `hosts`, mapped through `cms.contributors` to django-cast Contributor slugs, once the
  editor API accepts an episode `contributors` field (see below).
- **Transcript**: Voxhelm transcript generation, but only once diarization is enabled on the Audio and the
  contributors are set, so the transcript gets speaker labels.
- It prints the Wagtail preview and edit URLs, says plainly that nothing was published, and ends with the numbered
  list of manual steps that remain, in the order to do them (or "No manual steps remain.").

Re-running it updates the same draft instead of creating a second one:

- The episode and Audio ids are stored in `cms/draft.json` (per `cms.base_url` and resolved podcast parent), so the draft is found again even
  after the slug changed. Without that file the draft is looked up with `GET api/editor/posts/?parent=<id>&slug=<slug>`.
- The update is a `PATCH api/editor/episodes/<id>/` with the draft's `latest_revision_id` as `base_revision_id` and
  `require_unpublished: true`. It sends every field listed above again, so a re-run replaces edits made to them in
  Wagtail with the workspace copy (make lasting changes in the workspace, or stop re-running `cms-draft` once you
  edit in Wagtail). django-cast refuses the update when the draft changes between reading and writing it (re-run) and
  when the episode is already live; `cms-draft` never changes a published episode.
- The Audio is reused: an Audio uploaded by a run whose episode request then failed (stored in `cms/draft.json`
  until it is attached), else the draft's `podcast_audio`. The editor API cannot change an Audio, so its title,
  subtitle, chapter marks and file stay as uploaded. `--new-audio` uploads the file again as a new Audio and points
  the draft at it; once the draft uses it, `cms-draft` says which old Audio to delete in Wagtail.
- Every run reads the Audio from the editor audio list (paged by number on the configured site) and checks what is
  still missing: further formats, diarization mode. The steps for them are listed again until they are done. The
  transcript of a new Audio stays pending in `cms/draft.json` until it is started or listed as a manual step, so a
  run that failed after the upload does not lose it.

Limitations of the current django-cast editor API. `cms-draft` detects each missing capability on every run, uses
the endpoint as soon as a django-cast release offers it, and until then lists the manual step:

1. **Further audio formats.** There is no endpoint to add files to an existing Audio, so the M4A, Ogg Vorbis and Opus
   outputs next to the MP3 must be added in the Wagtail audio editor (URL and file paths are printed). `cms-draft`
   asks `OPTIONS api/editor/media/audios/<id>/` whether `PATCH` is allowed; once it is, each format goes there as its
   own multipart request (field `m4a`/`oga`/`opus`).
2. **Diarization mode.** Speaker labels need `transcript_diarization_mode` = Enabled on the Audio. The upload API
   rejects `enabled` today, and `inherit` follows the site's Voxhelm settings (on python-podcast.de these currently
   switch diarization off even though the environment enables it). Before uploading, `cms-draft` probes with a
   file-less upload request. The editor's `required` diagnostic "Submit one audio file." in `non_field_errors`
   confirms support and `enabled` is sent with the upload. An explicit mode rejection uploads with the site
   default and sets the mode with the audio `PATCH` above, or lists "Set Transcript diarization mode to Enabled"
   in the audio editor. Ambiguous validation, including an empty error map or a collection error, stops before
   upload and reports that support is indeterminate. The Audio's current mode is read from the editor
   audio list on every run, so a mode set by hand counts, and a mode set through the API counts only when the
   response reports it. When the Audio is not in the list, checking its formats and its mode are manual steps
   (nothing is uploaded blindly).
3. **Contributors.** The editor API cannot set episode contributors yet; Voxhelm uses them for the speaker count and
   the known speakers. `cms-draft` sends `contributors: [{"slug": ..., "role": "host"|"guest"}]` in a follow-up
   `PATCH` once episode responses contain a `contributors` field; until then it lists the contributors to add in the
   episode editor. The contributors are confirmed from the `PATCH` response; a response without them is a manual
   step too. Hosts without a `cms.contributors` entry, and an `episode.yaml` without `hosts`, are always a manual
   step (add the entries to automate it; guests go into `hosts` and get a `cms.contributors` entry with
   `role: guest` in `episode.yaml`).
4. **Transcript.** There is no editor endpoint to start Voxhelm transcript generation yet. `cms-draft` probes
   `OPTIONS api/editor/episodes/<id>/transcript/` and, once `POST` is allowed, enqueues the transcript of a new
   Audio (`--transcript` also requests it for a reused Audio) - but only when the Audio reports diarization mode
   Enabled and the contributors are confirmed. Otherwise the last manual step is "Generate transcript" in the episode
   editor, after the steps above; when the endpoint exists, the transcript stays pending and a re-run starts it once
   the steps are done.

`podcast cms-publish` finds the same draft, shows its title, slug, status, Audio and preview URL, asks for
confirmation (`--yes` skips the question) and publishes it with `POST api/editor/episodes/<id>/publish/` and the
draft's revision in `If-Match`, so a revision edited after the summary was shown is not published. The token needs the
`publish` scope if it is scoped.

Episode detail, update and publish responses must identify the requested episode. A detail mismatch stops the
workflow before it uses the response. If an update or publish response identifies a different episode, check the
requested episode in Wagtail before retrying because the result of the write is uncertain.

Security: the token is read from the environment variable named by `cms.token_env` (default
`PODCAST_PIPELINE_CMS_TOKEN`) and sent as `Authorization: Token <key>`. `cms.base_url` must be `https://`; every
request goes to it, and redirects are never followed (a redirect fails the run), so the token is never sent anywhere
else. `--dry-run` neither reads the token nor calls the site. Concurrent `cms-draft`/`cms-publish` runs for the same
workspace are refused (`cms/.cms.lock`).

## Local web UIs (pick --web, dashboard)

`podcast pick --web` and `podcast dashboard` serve a local UI on `http://127.0.0.1:<random port>/`. Both accept only
requests meant for themselves, so other web pages open in the browser cannot drive them (for example to start a paid
Auphonic production):

- The `Host` header must be `127.0.0.1`, `localhost` or `[::1]` with the server's own port; anything else gets `403`.
  This also blocks DNS-rebinding attacks.
- Every request needs this run's session token. Each start draws a new random token for
  `http://127.0.0.1:<port>/?token=<token>` and prints that URL to the terminal. The URL never goes into a
  command-line argument that other users could list. When Python's `webbrowser` module selects its macOS
  `osascript` controller (which controller it picks depends on the platform, the Python version and `$BROWSER`),
  that controller receives the URL on stdin. In every other case, and if that controller fails, the UI tries to
  open a private launch file (mode `0600` in a temporary directory) that forwards to the URL. If no browser
  opens, the UI keeps running and says so on the terminal: open the printed URL yourself. The launch file is
  removed when the UI closes; a run that is killed (for example with `SIGTERM`) can leave it behind, but its
  token stopped working with that server. The first request sets an `HttpOnly`, `SameSite=Strict` cookie and
  forwards to `http://127.0.0.1:<port>/`. Requests without that cookie or the token header get `403`, so a local
  program or another user that does not know the token cannot read or drive the UI.
  Limits: the token is visible in the terminal output, and the browser also sends the cookie to other services on
  `127.0.0.1` that it visits (cookies are not port-specific), so such a service could learn the token of a running
  UI. The token is new for every run and dies when the UI closes.
  If the UI shows the `403` message, or you opened it through `localhost`, open the printed URL again.
  Scripts send the token as a header: `curl -H 'X-Session-Token: <token>' http://127.0.0.1:<port>/api/status`.
- State-changing requests (`POST`, `PUT`, `PATCH`, `DELETE`) must be same-origin: an `Origin` header must equal the
  UI's own origin and `Sec-Fetch-Site`, when sent, must be `same-origin` or `none` (otherwise `403`).
- State-changing requests must send `Content-Type: application/json` (otherwise `415`), including body-less `DELETE`s.
  Scripts calling the API directly (for example with `curl`) need `-H 'Content-Type: application/json'`.
- Every response sends `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`,
  `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`, so other sites cannot embed the UI in a
  frame and trick clicks on its buttons.

The dashboard's **Close** button keeps the dashboard open and shows the active stage while a background job
is running. Job progress remains visible. After the job completes or fails, click **Close** again.
If the dashboard server has already stopped (for example after Ctrl-C), **Close** shows the closed page
instead of an error.
`POST /api/done` returns 409 with the active job ID in `job_id` while a job is running. Once it accepts
shutdown, new background job requests receive 503.

The standalone picker reads authoritative selections and refreshed candidate membership whenever it loads
assets. Each response uses one workspace snapshot for candidate membership and selections. Preview and
tag-pool rendering run after the workspace lock is released. Custom drafts survive these refreshes and asset
switches in the current page. A save acknowledges the submitted revision. Text typed while saving stays
unsaved. Candidate and custom selections admit one pending choice per asset. After an uncertain mutation,
the picker reloads stored state before allowing another choice and leaves the decision to retry to you.
Each Select sends a digest of the candidate copy its card shows. If that candidate has changed or
disappeared since, even when another picker tab already loaded the new copy, the server refuses the
choice and the picker reloads the candidates and asks you to choose again.

Every picker browser exchange has a thirty-second deadline covering response headers and body, except
refinement: its deadline is the configured drafter agent timeout plus one minute, so normal refinements finish
inside it. A timeout of a mutation means its outcome is unknown: check the stored selection before deliberately
retrying. If the picker cannot render the Markdown preview, the refinement still returns the saved
candidate with a plain-text preview. After a failed or timed-out refinement the picker reloads the assets
to show any candidate the server published meanwhile. Check for it before starting more paid work.
Pending controls are released on failure, drafts remain available, and errors stay with their asset.
Done waits for tag saves and is unavailable during refinements or pending choices. The server also refuses
Done while refinement work is running. Once it accepts Done, it shuts down even if the acknowledgment
cannot reach the browser. After thirty minutes the server stops accepting work and waits for accepted
handlers to finish, including the configured agent timeout. Accepted handlers keep running if you close
the browser.

Sidebar assets are native buttons: Tab/Shift-Tab reaches them and Enter/Space activates them. Selection and
tag updates retain the equivalent control's focus, and background saves preserve the editor's text and caret.
Composing or already-handled Enter in tag/refinement inputs never submits; committed Enter and explicit
buttons still work. See [standalone picker limits](../reference/configuration.md#standalone-picker-limits).

Each dashboard status refresh reads the latest workspace state, including selections saved by other
CLI processes or dashboards. A missing candidates directory counts as empty. If a candidate read fails,
status displays an error and the next refresh tries again. Candidate selections count only while the
same asset still has that candidate and usable published text. Assets with stale candidate selections
remain in the total as unselected. Saved manual text also counts as a selection. The Pick tab check mark
uses the same rule against its loaded candidate list.

The dashboard counts chunks from the published manifest when it passes structural validation. This status
counts the entries recorded at publication. Drafting separately verifies the source and chunk contents.
Without a usable manifest, status counts regular `chunk_*.txt` files and skips symlinks. It scans
at most 8,192 directory entries and checks one extra entry to detect overflow. An oversized or unreadable
inventory displays “Count incomplete” and returns `null` in `/api/status` at `stages.chunks`. A missing chunk directory reports zero.

Saving or clearing an asset's editorial notes validates the stored `episode.yaml` first. If its
`editorial_notes` value is malformed, the dashboard returns HTTP 400 and preserves the file for repair.
An absent notes field starts empty. An edit to a valid notes map preserves the other assets' entries.

The dashboard also runs each long job single-flight:

- While a `produce`, `transcribe`, `draft`, `summarize` or `candidates` job, or a `regenerate`, `refine` or `review`
  job for the same asset is running, starting another one returns `409` with the running job's `job_id`.
- Agent CLI calls inside these jobs time out after 15 minutes by default (`PODCAST_PIPELINE_AGENT_TIMEOUT` or
  `agents.<role>.timeout_seconds` change it; see `reference/configuration.md`). A timed-out call ends the job as
  `failed` with `<Role> CLI timed out after N s`, and the stage accepts a new request.
- `podcast produce` itself holds an exclusive lock (`auphonic/.produce.lock`) for the whole Auphonic run. A second run
  for the same workspace, from the CLI or a dashboard, fails instead of starting a second production. A rerun after a
  failure reuses the production UUID stored in `state.json` rather than starting a new one; only
  `podcast produce --restart` replaces it (`--update` re-runs that same production under the same lock).
  Other `state.json` writers (pick, dashboard, review loop) never clear or replace that stored UUID or the production
  history (`auphonic_productions`), and all `state.json` updates are serialized with a lock on the workspace
  directory.
- Candidate counts (`candidates`) are clamped to 1-10 and review `max_iterations` to 1-10.

## Episode workspace layout

```
ep_001/
  episode.yaml
  state.json
  transcript/
    transcript.txt
    chapters.txt
    chunks/
      chunk_0001.txt
      chunk_0001.json
  summaries/
    chunks/
      chunk_0001.summary.json
    episode/
      episode_summary.json
      episode_summary.md
      episode_summary.html
  copy/
    candidates/<asset_id>/candidate_<uuid>.{json,md,html}
    reviews/<asset_id>/iteration_XX.<reviewer>.json
    protocol/<asset_id>/iteration_XX.{json,creator.json}
    protocol/<asset_id>/state.json
    selected/<asset_id>.{md,html,txt}
    provenance/<kind>/<ref>.json
  auphonic/
    downloads/
    outputs/
  cms/
    draft.json        # django-cast podcast parent, episode and Audio ids (podcast cms-draft)
```

## Copy/paste HTML into Wagtail

Use the HTML files produced by `podcast pick` (or by a converged review loop) when pasting into Wagtail RichText
fields.

1. Open `copy/selected/<asset_id>.html`.
2. In Wagtail, switch the RichText field to its HTML/source mode.
3. Paste the HTML and save.

The HTML is generated deterministically from Markdown and supports headings, paragraphs, lists, links, inline code, and
emphasis. If you need plain text, use the `.txt` output instead.

Link URLs may contain balanced parentheses, so links such as
`[Python](https://en.wikipedia.org/wiki/Python_(programming_language))` stay whole. A single `*` only starts emphasis
before non-whitespace and not between two letters or digits, so `2*3*4` and `a * b * c` stay literal; intraword
emphasis such as `foo*bar*` is not supported.

### Subprocess output and transcription limits

Agent and transcription commands have independent 8 MiB stdout and 1 MiB stderr byte ceilings.
Overflow fails the step and kills/reaps the owned process group, as do timeout and interruption.
Successful agent and transcription commands are retired the same way, so helpers left in their
session/process group do not survive, and uncertain cleanup fails the step;
transcription nonzero-exit diagnostics retain only the final 4096 characters. Agent errors omit raw stderr.
Agent stdin must be completely delivered and fit 1 MiB of UTF-8. Successful agent stdout must be valid UTF-8.
On nonzero exit, malformed stdout bytes are decoded with replacement within the same byte limit, preserving the exit code.
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
If a worker exits without recording an outcome, job polling and streaming report a failed job
with the message `Background worker exited unexpectedly`. The stage can then be retried.
Each job retains the latest 128 KiB / 1024 progress entries. Oversized writes retain a UTF-8 tail
prepared before acquiring the dashboard lock.
JSON/SSE responses include an earlier-progress-truncated notice when history was evicted.
SSE progress IDs increase monotonically and support `Last-Event-ID` reconnects across eviction.
A cursor ahead of the job's newest event replays the retained history and resumes live progress.
The dashboard retains at most 64 terminal jobs plus all running jobs; evicted job URLs return 404.
Both local UIs admit an explicit Host port only as one to five ASCII decimal digits in 1..65535.
Malformed ports return the existing 403 Forbidden host response before reading a body, including
oversized numbers, Unicode digits and out-of-range values. IPv4/IPv6 loopback hosts remain supported.
Workspace reads/writes, job admission and context-lock acquisition run in Starlette's worker pool.
Status reads workspace state, inventory and selected files under the publication lock. Job snapshots
are copied under the context lock. HTTP/SSE sends and waits happen after those locks are released.
A slow workspace operation or a worker-held lock can delay snapshots and the interactive shutdown
check while the event loop remains responsive. Initialization and ingest use this boundary too.

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

### Conflicting local dashboard work

A running draft, summarize, candidates or transcribe job blocks the other three stages and all asset
review/regenerate/refine jobs. Asset operations block other operations on the same asset, while different
assets remain independent. Production has separate single-flight admission. Conflicts return HTTP 409 with
the running stage and job ID, displayed as an error in the dashboard; retry after that job finishes or fails.
Admission is checked together with job creation under the dashboard context lock, including launch failures.
This is a local dashboard policy, not a cross-process scheduler or a lock for independent CLI commands.

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

## Inspect workspace status

`podcast status --workspace <episode>` is read-only. Status JSON must be coherent regular UTF-8
workspace files (no symlink components), at most 8 MiB each and 64 JSON container levels deep.
An inspection admits at most 10,000 directory entries (including ignored names and repeated scans)
and 32 MiB of file contents. Exceeding an aggregate limit reports **inspection incomplete** with
exit code 1 and omits the checklist; split or clean up the workspace before retrying.
Chunk, chunk-summary and candidate inventory counts use regular files in workspace directories.
Directories, symlinks and special files matching an artifact name leave that evidence missing
unless a regular file also matches.
Invalid state blocks selection readiness; invalid review evidence stops status with a concise error.
Review protocol folders must match a supported asset ID and supply unique evidence for that asset.
Status uses the review engine's persisted protocol contract: contiguous strict integer iterations,
matching creator/reviewer records, and a terminal decision matching the latest terminal review.
A `converged` label alone, a missing final review or a contradictory decision is invalid evidence.
Selection readiness requires the canonical, nonblank selected file in the recorded candidate's
format and matching its content in `state.json`; unrelated stems, superseded formats and stale
text do not count. Custom text uses the asset's canonical custom format and its manual-selection
provenance marker. When a tag asset has no candidate selection recorded, status accepts the
picker's canonical Markdown, including its heading-only output for an explicitly empty selection.
iTunes keywords in that file must be comma-free.
Status lines and CLI picker previews escape terminal controls (including ESC, OSC payload delimiters,
CR and C1) as visible text. Status lines are capped at 1,000 characters and candidate previews at 72;
these display limits never change the stored copy or review evidence.

The terminal picker binds each displayed candidate to its canonical JSON bytes and file generation
(regular UTF-8 JSON within 8 MiB). It rechecks that evidence inside the workspace update lock before
publishing. If the candidate was regenerated, replaced or deleted while you were choosing, the
previous selection survives: reload candidates by running `podcast pick` again and make a fresh choice.
Even replacement with identical bytes requires a fresh choice. Workspace writers share this lock;
manual filesystem edits after the final comparison are outside that coordination guarantee.
Each successful asset prints `Selected <asset>: <path> (candidate=<id>)` immediately after commit.
Cancellation or a later failure prints `Pick incomplete: <saved>/<total> assets committed` and exits
unsuccessfully. Earlier receipts remain saved; inspect the current selection before continuing.
