# Episode workflow: init → ingest → draft → review → pick → produce

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
generation) to prevent hallucinated speaker names.

When the workspace already has `transcript/transcript.txt` from `podcast transcribe`, `podcast draft` reuses it. Pass
`--transcript /path/to/file.txt` only when creating a new workspace from an external transcript or when you want to
replace the transcript currently stored in the workspace.

Outputs:

- `transcript/` contains the ingested transcript + chunk files.
- `summaries/` contains chunk summaries and the episode summary.
- `copy/candidates/<asset_id>/` contains candidate JSON + Markdown + HTML files.

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
- When the loop converges, the selected draft is written to `copy/selected/<asset_id>.*`.

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
- Without `--web`, the CLI prompts when multiple candidates exist and writes the selection to `copy/selected/`.
- Use `--asset-id` and `--candidate-id` to pick a specific candidate non-interactively (CLI only).
- A selection has one canonical file: `.md` (with a derived `.html`), `.txt` or `.html`. Selecting a candidate in
  another format removes the files of the previous format after the new ones are written, so `copy/selected/` never
  keeps a stale `.html` or `.md` next to a newer selection. If writing fails, the previous canonical file is unchanged;
  if only the final removal fails, the command reports it and repeating the selection finishes the switch.

### Write or edit the selected text

When no candidate is quite right (for example you want the slug `python-315` and none of the candidates offers it),
write the selection yourself. Both `podcast pick --web` and the dashboard's Pick tab show a "Custom text" card below
the candidates of each asset (in the dashboard, tag assets keep their per-tag editor instead):

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
```

Notes:

- Credentials: `AUPHONIC_API_KEY` (an API key from your Auphonic account settings, sent as a Bearer token), or
  `AUPHONIC_USER` and `AUPHONIC_PASSWORD` (HTTP Basic). The API key wins when both are set. `AUPHONIC_BASE_URL`
  overrides the API base URL. Credentials are only sent to that URL's origin, never to output download hosts on
  other origins.
- The preset must be an Auphonic multitrack preset (created in Auphonic's multitrack mode). Single-track presets, such
  as a mono preset built for one pre-mixed file, do not apply to multitrack productions. Before creating a production,
  `produce` reads the preset and stops on a type mismatch, so nothing is created or paid for.
- The multitrack preset must not contain speaker tracks: Auphonic appends the preset's tracks to the production's, which
  duplicates them ("Multiple tracks with ID ... found"). `produce` stops before creating a production when the preset
  has any; put per-track algorithms into `auphonic.tracks[].algorithms` instead.
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
  missing.
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
- `--update` and `--restart` cannot be combined. `--update --dry-run` prints `production_uuid`, `production_update`
  and `track_updates` without calling the API (it still needs a stored production UUID).

Interrupted output downloads remove partial temporary files and preserve an existing destination.
Stream and filesystem failures surface as `AuphonicApiError`, so the production can be retried.

## Local web UIs (pick --web, dashboard)

`podcast pick --web` and `podcast dashboard` serve a local UI on `http://127.0.0.1:<random port>/`. Both accept only
requests meant for themselves, so other web pages open in the browser cannot drive them (for example to start a paid
Auphonic production):

- The `Host` header must be `127.0.0.1`, `localhost` or `[::1]` with the server's own port; anything else gets `403`.
  This also blocks DNS-rebinding attacks.
- State-changing requests (`POST`, `PUT`, `PATCH`, `DELETE`) must be same-origin: an `Origin` header must equal the
  UI's own origin and `Sec-Fetch-Site`, when sent, must be `same-origin` or `none` (otherwise `403`).
- State-changing requests must send `Content-Type: application/json` (otherwise `415`), including body-less `DELETE`s.
  Scripts calling the API directly (for example with `curl`) need `-H 'Content-Type: application/json'`.

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
  Other `state.json` writers (pick, dashboard, review loop) never clear or replace that stored UUID, and all
  `state.json` updates are serialized with a lock on the workspace directory.
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
