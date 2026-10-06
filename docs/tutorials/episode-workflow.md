# Episode workflow: init → ingest → draft → review → pick → produce

This guide ties together the MVP pipeline steps for a single episode and shows where each step writes in the
workspace.

## 1. Initialize a workspace (init)

```bash
podcast init --episode-id ep_001 --workspace ./workspaces/ep_001
```

Notes:

- `--workspace` must not exist; the command creates it.
- If you omit `--workspace`, the default is `./episodes/<episode_id>`.

## 2. Export and transcribe audio

**Export a master mix** from Ultraschall/Reaper to MP3 first. The per-track FLAC files in the Reaper media folder are
not suitable for transcription unless one track is explicitly marked as the mix source. `podcast transcribe` expects a
single audio input and prefers `auphonic.input_file` from `episode.yaml`. The recommended transcription path is
`podcast-transcript` with the `voxhelm` backend.

```bash
# Example episode.yaml snippet
cat >> ./workspaces/ep_068/episode.yaml <<'YAML'
auphonic:
  input_file: /Users/jochen/Documents/REAPER Media/pp_068/pp_068.mp3
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
  transcript. Re-running on unchanged audio reuses that cache; see the `auphonic` notes in the episode.yaml reference.
- If the workspace has multiple possible audio inputs, set `auphonic.input_file` explicitly before running the command.
- If you want to override the backend or pass extra transcription flags, add more `--arg` entries; for example
  `--arg=--language --arg=de`.

## 3. Draft text assets (draft)

`podcast draft` runs the transcript chunking + summary + candidate generation pipeline. It reuses an existing workspace
if one is present (clearing stale chunks/summaries on re-run).

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

## 6. Produce with Auphonic (produce)

`podcast produce` builds the Auphonic payload from `episode.yaml`, the global config and the selected copy, starts an
Auphonic production, waits for it and downloads the outputs to `auphonic/outputs/`. A production costs Auphonic
credits, so preview the payload first:

```bash
podcast produce --workspace ./workspaces/ep_001 --dry-run   # print the payload, no API call
podcast produce --workspace ./workspaces/ep_001             # start, wait, download
podcast produce --workspace ./workspaces/ep_001 --restart   # discard the stored production, start a new one
```

Notes:

- Credentials: `AUPHONIC_API_KEY` (an API key from your Auphonic account settings, sent as a Bearer token), or
  `AUPHONIC_USER` and `AUPHONIC_PASSWORD` (HTTP Basic). The API key wins when both are set. `AUPHONIC_BASE_URL`
  overrides the API base URL. Credentials are only sent to that URL's origin, never to output download hosts on
  other origins.
- A production takes exactly one input file (`auphonic.input_file`, a one-entry `auphonic.input_files`, or a single
  preferred mix/master/final track). More than one input fails; multitrack productions are not supported.
- The input is a local path (relative to the workspace is ok) or an `http(s)://` URL. `produce` follows Auphonic's JSON
  API flow: create the production (an input URL goes into this request), upload a local file to
  `production/{uuid}/upload.json`, then call `production/{uuid}/start.json`.
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
  the same status; fix the cause, then use `--restart` to start a new production on purpose. The dashboard's produce
  button always resumes; use the CLI for `--restart`.

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

- While a `produce`, `transcribe`, `draft`, `summarize` or `candidates` job, a `regenerate` job for the same asset, or a
  `review` job for the same asset is running, starting another one returns `409` with the running job's `job_id`.
- `podcast produce` itself holds an exclusive lock (`auphonic/.produce.lock`) for the whole Auphonic run. A second run
  for the same workspace, from the CLI or a dashboard, fails instead of starting a second production. A rerun after a
  failure reuses the production UUID stored in `state.json` rather than starting a new one; only
  `podcast produce --restart` replaces it.
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
