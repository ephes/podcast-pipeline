# Run the review loop and read status

This guide shows how to run the MVP-1 creator/reviewer loop with the fake runner and how to interpret the `podcast status` output.

## 1. Run the loop

By default, `podcast review` uses the local Codex/Claude CLIs configured in `episode.yaml` or
`~/.config/podcast-pipeline/config.yaml` and writes protocol state under `copy/protocol/<asset_id>/`.

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

- `--workspace` must not exist; the command creates it.
- `--asset-id` must match `^[a-z][a-z0-9_]*$`.
- Agent CLI configuration is documented in `reference/configuration.md`.
- On convergence, the command prints the selected asset path.
- Review iterations are written under `copy/reviews/<asset_id>/iteration_XX.<reviewer>.json` when the reviewer label is set.
- The loop stops only when the reviewer returns `ok` and the creator reports `done`, or when `max_iterations` is reached.

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
