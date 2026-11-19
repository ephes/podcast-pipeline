# Run the review loop and read status

This guide shows how to run the MVP-1 creator/reviewer loop with the fake runner and how to interpret the `podcast status` output.

## 1. Run the loop

By default, `podcast review` uses the local Codex/Claude CLIs configured in `episode.yaml` or
`~/.config/podcast-pipeline/config.yaml` and writes protocol state under `copy/protocol/<asset_id>/`.

Prompt receipts under `copy/provenance/prompts/` are addressed by a SHA-256 digest of their
canonical JSON fields, including the template name, rendered text, context, glossary and examples.
Identical receipts reuse the same reference. Older references still resolve to their original files.

```bash
podcast review \
  --workspace ./workspaces/ep_001 \
  --episode-id ep_001 \
  --asset-id description \
  --max-iterations 3
```

If you do not have the CLIs installed, use the fake runner instead:

```bash
podcast review \
  --fake-runner \
  --workspace ./workspaces/ep_001 \
  --episode-id ep_001 \
  --asset-id description \
  --max-iterations 3
```

Notes:

- `--workspace` can name an existing initialized workspace. Repeating the command resumes
  `copy/protocol/<asset_id>/state.json` with the same `--max-iterations`; completed rounds
  are not sent to the agents again. Locked terminal runs make no agent calls or selection changes;
  the CLI and the dashboard job say so ("no agent calls") instead of reporting a fresh result.
  A selection conflict, invalid protocol state or unreadable saved candidate evidence ends
  `podcast review` with a "Review stopped" message and exit code 1, not a traceback.
- Each completed candidate/review is checkpointed before the next agent call. After an agent
  failure, fix the cause and repeat the command. Invalid protocol state stops before agent calls.
- For an intentional fresh run, archive `copy/protocol/<asset_id>/` and
  `copy/reviews/<asset_id>/` outside their active paths (for example, under
  `review-archive/<timestamp>/<asset_id>/`) before repeating the command. Keep the candidate
  history and workspace state: a fresh run can use the latest candidate as a seed.
  Do not delete paid review evidence.
- `--asset-id` must match `^[a-z][a-z0-9_]*$`.
- Agent CLI configuration is documented in `reference/configuration.md`.
- On convergence, the command prints the selected asset path.
- Review iterations are written under `copy/reviews/<asset_id>/iteration_XX.<reviewer>.json` when the reviewer label is set.
- The loop stops when the reviewer returns `ok` and the creator reports `done`, immediately
  when the reviewer requests `needs_human`, or at `max_iterations`. Human decisions stay locked on resume.
- If a producer changes the selected text, format, candidate or manual provenance while review runs,
  automatic selection stops with a conflict. The newer selection and completed review evidence survive;
  inspect them with `podcast pick` before deliberately starting a fresh run.

## 2. Inspect loop status

```bash
podcast status --workspace ./workspaces/ep_001
```

Example output:

```
Workspace: /absolute/path/to/workspaces/ep_001
Asset: description
  Iteration: 2/3
  Verdict: ok
  Outcome: converged
  Blocking issues: none
  Outstanding issues: none
```

If no protocol state files exist, `podcast status` reports that no state was found under `copy/protocol/`.

## 3. Status fields

- `Asset`: the asset id the loop is drafting.
- `Iteration`: `current/max`; `0/max` means no iterations are recorded yet.
- `Verdict`: the latest reviewer verdict (`ok`, `changes_requested`, `needs_human`, or `none`).
- `Outcome`: `in_progress`, `converged`, or `needs_human`; a decision reason appears as `Outcome: ... (reason=...)`.
- `Blocking issues`: count of issues with `severity=error`.
- `Outstanding issues`: total issue count plus per-issue lines with severity, message, and optional `code` or `field`.

For initialization failures, cleanup and retry behavior, see
[Initialize a workspace](../tutorials/episode-workflow.md#1-initialize-a-workspace-init).
