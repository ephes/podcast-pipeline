# Run the text pipeline

This guide walks through the text pipeline: chunk a transcript, summarize it, and generate candidate assets. By
default, `podcast draft` uses the configured local drafter CLI for real LLM-backed output. Add `--dry-run` when you
want deterministic stub output without external agent calls.

## 1. Prepare inputs

- A transcript plain-text file (UTF-8).
- Optional: a chapters text file with one chapter per line.

## 2. Run the complete pipeline

Run summaries and candidates in one step:

```bash
podcast draft \
  --workspace ./workspaces/ep_001 \
  --transcript /path/to/transcript.txt \
  --chapters /path/to/chapters.txt \
  --episode-id ep_001 \
  --host Jochen \
  --host Dominik \
  --candidates 3
```

Notes:

- A new workspace requires `--transcript`; an existing workspace can reuse `transcript/transcript.txt`.
- `--host` is repeatable. Stored host names are reused on later runs and included in the LLM prompts.
- The configured drafter CLI defaults to Claude and can be overridden through the agent configuration.
- Existing chunks and summaries are reused unless you supply a replacement transcript.

For a deterministic local smoke test, add `--dry-run`. The dry-run workspace must not already exist.

## 3. Run individual stub stages

The lower-level `summarize` and `draft-candidates` commands remain useful for deterministic tests and demonstrations:

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

Then generate stub candidates:

```bash
podcast draft-candidates --workspace ./workspaces/ep_001 --candidates 3
```

## 4. (Optional) Add chapters

Chapters are used when generating assets. Supply them using any of these sources (first match wins):

- Put `transcript/chapters.txt` inside the workspace.
- Set `inputs.chapters` in `episode.yaml`.
- Pass `--chapters /path/to/chapters.txt` to the next step.

## 5. Inspect outputs

- Chunk text + metadata: `transcript/chunks/chunk_0001.txt` and `.json`.
- Chunk summaries: `summaries/chunks/chunk_0001.summary.json`.
- Episode summary: `summaries/episode/episode_summary.{json,md,html}`.
- Candidate assets: `copy/candidates/<asset_id>/candidate_<uuid>.{json,md,html}`.

Use `podcast pick --workspace ./workspaces/ep_001 --web` or the dashboard to select final candidates.
