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
- A replacement transcript is copied completely before anything is removed, so an unreadable source or a failed copy
  leaves the current transcript, chunks and summaries untouched. Passing the workspace's own
  `transcript/transcript.txt` is a no-op. See the episode workflow tutorial for the exact ordering.

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
- Set `inputs.chapters` in `episode.yaml`. For dashboard regeneration, use a workspace-relative path
  such as `transcript/chapters.txt` to an existing regular file. Absolute paths, `..` components and
  symlinks escaping the workspace are rejected before the agent starts.
- Pass `--chapters /path/to/chapters.txt` to the next step.

Dashboard regeneration reads only `inputs.chapters`; without it, no chapters are passed.

## 5. Inspect outputs

- Chunk text + metadata: `transcript/chunks/chunk_0001.txt` and `.json`.
- Chunk summaries: `summaries/chunks/chunk_0001.summary.json`.
- Episode summary: `summaries/episode/episode_summary.{json,md,html}`.
- Candidate assets: `copy/candidates/<asset_id>/candidate_<uuid>.{json,md,html}`.

Use `podcast pick --workspace ./workspaces/ep_001 --web` or the dashboard to select final candidates.

For initialization failures, cleanup and retry behavior, see
[Initialize a workspace](../tutorials/episode-workflow.md#1-initialize-a-workspace-init).

### Re-transcribing an existing workspace

Successful transcription publishes canonical `transcript.txt` and optional `chapters.txt` through sibling
atomic replacements. The complete mode output is validated before publication. Handled canonical, YAML or
provenance write failures restore the previous canonical generation; the previous mode output is restored as well so mode pointers and provenance continue to agree. An unchaptered result removes `inputs.chapters`, `inputs.chapters_<mode>` and the
canonical chapter file while retaining other modes' files and pointers. This retirement includes manually
imported chapters from `draft --chapters`: canonical chapters always accompany the latest transcript, so pass
`--chapters` again on the next draft if the same source chapters should be reused.

Draft chunk manifests bind the canonical transcript bytes, so changing those bytes invalidates reuse of
chunks and their episode summary. The next draft rebuilds them; identical transcription output keeps the
completed caches. Failed publication preserves the old canonical source and its usable caches. These
recoveries cover handled errors, not process death between multiple file replacements; see
`../explanation/text-pipeline.md` for completion validation and retained backup limits.

### Selection commit failures

Candidate and custom-text saves reload current state under the stable workspace state lock, prepare and
validate the intended metadata, and preserve the affected selected text formats, manual provenance and
`state.json` before publishing. A handled provenance, serialization or final state-replacement failure restores
the previous usable selection. Retry after fixing the write error. A recovery error names retained sibling
backups; repair those before retrying. This is rollback for handled failures, not a multi-file rename transaction:
process death can leave mixed files and backups, requiring inspection against `state.json` and provenance.
