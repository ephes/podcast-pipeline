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

## 2. Ingest Reaper media (ingest)

```bash
podcast ingest \
  --workspace ./workspaces/ep_001 \
  --reaper-media-dir /path/to/ReaperMedia \
  --tracks-glob "*.flac"
```

Notes:

- Ingest updates `episode.yaml` with `sources` and `tracks`; the audio stays in place.
- Skip this step if you only need text assets from a transcript.

## 3. Draft text assets (draft)

`podcast draft` runs the transcript chunking + summary + candidate generation pipeline. It creates a new workspace, so
use a fresh path if one already exists.

```bash
podcast draft \
  --dry-run \
  --workspace ./workspaces/ep_001_text \
  --transcript /path/to/transcript.txt \
  --chapters /path/to/chapters.txt \
  --episode-id ep_001 \
  --candidates 3
```

Outputs:

- `transcript/` contains the ingested transcript + chunk files.
- `summaries/` contains chunk summaries and the episode summary.
- `copy/candidates/<asset_id>/` contains candidate JSON + Markdown + HTML files.

## 4. Run the review loop (review)

```bash
podcast review \
  --workspace ./workspaces/ep_001_text \
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
podcast pick --workspace ./workspaces/ep_001_text
```

Notes:

- `podcast pick` prompts when multiple candidates exist and writes the selection to `copy/selected/`.
- Use `--asset-id` and `--candidate-id` to pick a specific candidate non-interactively.

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
