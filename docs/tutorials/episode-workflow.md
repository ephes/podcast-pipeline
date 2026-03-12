# Episode workflow: init → ingest → draft → review → pick

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
single audio input and prefers `auphonic.input_file` from `episode.yaml`.

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

- `podcast transcribe` imports the generated plain-text transcript into `transcript/<mode>/transcript.txt` inside the workspace.
- `podcast-transcript` still keeps its own artifacts under `TRANSCRIPT_DIR` (default `~/.podcast-transcripts/transcripts/`).
- If the workspace has multiple possible audio inputs, set `auphonic.input_file` explicitly before running the command.

## 3. Draft text assets (draft)

`podcast draft` runs the transcript chunking + summary + candidate generation pipeline. It reuses an existing workspace
if one is present (clearing stale chunks/summaries on re-run).

```bash
podcast draft \
  --workspace ./workspaces/ep_068 \
  --transcript ~/.podcast-transcripts/transcripts/pp_68/pp_68.txt \
  --episode-id ep_068 \
  --host Jochen --host Dominik \
  --candidates 3
```

The `--host` flag is repeatable and persists host names to `episode.yaml`. On subsequent runs without `--host`, the
stored names are reused automatically. Host names are injected into all LLM prompts (summarization and candidate
generation) to prevent hallucinated speaker names.

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
