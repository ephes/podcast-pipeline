# Run the text pipeline

This guide walks through the current text pipeline in this repo: chunk a transcript, generate stub summaries, then generate candidate assets. The commands only support stub/dry-run modes today (no external LLM calls).

## 1. Prepare inputs

- A transcript plain-text file (UTF-8).
- Optional: a chapters text file with one chapter per line.

## 2. Run chunking + summaries

If you want to run the full text pipeline (summaries + candidates) in one step, use:

```bash
podcast draft \
  --dry-run \
  --workspace ./workspaces/ep_001 \
  --transcript /path/to/transcript.txt \
  --chapters /path/to/chapters.txt \
  --episode-id ep_001
```

Notes:

- `--workspace` must not exist; the command creates it.

```bash
podcast summarize \
  --dry-run \
  --workspace ./workspaces/ep_001 \
  --transcript /path/to/transcript.txt \
  --episode-id ep_001
```

Notes:

- `--workspace` must not exist; it will be created.
- The transcript is copied into `transcript/transcript.txt` under the workspace.

## 3. (Optional) Add chapters

Chapters are used when generating assets. Supply them using any of these sources (first match wins):

- Put `transcript/chapters.txt` inside the workspace.
- Set `inputs.chapters` in `episode.yaml`.
- Pass `--chapters /path/to/chapters.txt` to the next step.

## 4. Generate candidate assets

```bash
podcast draft-candidates --workspace ./workspaces/ep_001 --candidates 3
```

```bash
podcast draft-candidates \
  --workspace ./workspaces/ep_001 \
  --chapters /path/to/chapters.txt
```

## 5. Inspect outputs

- Chunk text + metadata: `transcript/chunks/chunk_0001.txt` and `.json`.
- Chunk summaries: `summaries/chunks/chunk_0001.summary.json`.
- Episode summary: `summaries/episode/episode_summary.{json,md,html}`.
- Candidate assets: `copy/candidates/<asset_id>/candidate_<uuid>.{json,md,html}`.
