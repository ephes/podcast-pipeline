# MVP-0 domain model and review loop

This explanation documents the MVP-0 Creator/Reviewer loop and the domain objects it persists. It focuses on the
current implementation, workspace artifacts, and the convergence rules used by the loop engine.

## Domain model snapshot

The MVP-0 domain model centers on a workspace with assets that accumulate candidates and review iterations.

- `EpisodeWorkspace` is the root aggregate. It stores an `episode_id`, `root_dir`, and collections of `Asset`,
  `Chapter`, and `Track` records. It is serialized to `state.json` for persistence. Invariants enforce unique asset
  and track ids plus strictly increasing chapter start times.
- `Asset` groups all draft material for a single `asset_id` (for example `description`) and keeps:
  - `candidates` (`Candidate` objects) and `reviews` (`ReviewIteration` objects).
  - `selected_candidate_id` when the loop converges.
  - `kind` (optional) when the asset id matches a known `AssetKind`.
  - Invariants: `asset_id` values are unique per workspace, review iterations are strictly increasing, and
    `selected_candidate_id` must refer to an existing candidate.
- `Candidate` captures a single draft with `content`, `format`, `created_at`, and provenance metadata. Candidates are
  bound to an `asset_id` and get a UUID.
- `ReviewIteration` records the reviewer verdict (`ReviewVerdict`) and optional `ReviewIssue` list. Each issue carries
  a severity (`IssueSeverity`), message, and optional code/field. `ReviewVerdict.ok` cannot be combined with
  `IssueSeverity.error` issues. Reviews also carry a `reviewer` label, optional `summary`, and `provenance` entries to
  tag automated runs.
- `ReviewVerdict` is the tri-state verdict used by the loop engine: `ok`, `changes_requested`, or `needs_human`.

## Workspace layout and stored artifacts

Workspaces are rooted at an episode directory and follow the layout defined by `EpisodeWorkspaceLayout`. The CLI demo
creates a `./demo_workspace*` root, while helpers in `workspace_store` also support `./episodes/<episode_id>/`. The
review loop mostly writes under `copy/`, while the workspace state lives at the root.

```
<workspace>/
  episode.yaml
  state.json
  transcript/
    transcript.txt
    chapters.txt
    chunks/
      chunk_0001.txt
      chunk_0001.json
  auphonic/
    downloads/
    outputs/
  summaries/
    chunks/chunk_0001.summary.json
    episode/episode_summary.json
    episode/episode_summary.md
    episode/episode_summary.html
  copy/
    candidates/<asset_id>/candidate_<uuid>.json + .md/.html
    reviews/<asset_id>/iteration_01.json (or iteration_01.<reviewer>.json)
    protocol/<asset_id>/iteration_01.json
    protocol/<asset_id>/state.json
    selected/<asset_id>.md/.html/.txt
    provenance/<kind>/<ref>.json
```

Key persisted artifacts:

- `episode.yaml`: input metadata such as transcript/chapters paths.
- `state.json`: serialized `EpisodeWorkspace` snapshot.
- `copy/candidates/...`: JSON plus rendered text for each `Candidate`.
- `copy/reviews/...`: reviewer outputs per iteration.
- `copy/protocol/...`: loop protocol state (`LoopProtocolState`) plus per-iteration JSON envelopes
  (`LoopProtocolIteration`).
- `copy/selected/...`: selected final draft when converged.
- `summaries/...`: chunk and episode summaries written by the stub summarizer.
- `transcript/chunks/...`: transcript chunk text and metadata.
- `auphonic/...`: reserved for Auphonic downloads/outputs.

## Review loop flow and convergence rules

The loop is driven by `run_review_loop_engine` and follows a strict Creator/Reviewer cadence. It persists a
`LoopProtocolState` with a `LoopDecision` that can lock terminal outcomes to prevent replays from rewriting history.

1. Start from an optional existing protocol state; if it is terminal and the outcome is locked, the engine returns
   without re-running.
2. For each iteration up to `max_iterations`:
   - The creator receives `CreatorInput` (including the prior candidate/review) and returns a `Candidate` plus a
     `done` flag.
   - The reviewer receives the new candidate and returns a `ReviewIteration`.
   - The engine writes protocol JSON (`LoopProtocolIteration`) for the iteration and evaluates convergence.
3. The engine writes a protocol `state.json` snapshot for the loop.

Convergence is determined by the following rules:

- `ReviewVerdict.ok` + creator `done=True` terminates with outcome `converged`.
- If `iteration == max_iterations` without convergence, the loop ends with `needs_human` and reason `iteration_limit`.
- `ReviewVerdict.needs_human` is recorded on the iteration but does not terminate the loop on its own.

## Fake runner usage

The CLI `podcast review` command exposes a `--fake-runner` flag for the MVP-0 loop. When enabled, it uses the
`FakeCreatorRunner` and `FakeReviewerRunner` to emit scripted replies (including deterministic IDs and timestamps).
The fake runners can also mutate files via a `mutate_files` map in their JSON replies, which is useful for tests and
demo workspaces.

Example invocation:

```bash
podcast review --fake-runner --asset-id description --max-iterations 2
```

## References

- Domain model: `src/podcast_pipeline/domain/models.py`
- Workspace layout/store: `src/podcast_pipeline/workspace_store.py`
- Review loop engine: `src/podcast_pipeline/review_loop_engine.py`
- Fake runners: `src/podcast_pipeline/agent_runners.py`
- CLI + demo entrypoint: `src/podcast_pipeline/entrypoints/cli.py`, `src/podcast_pipeline/entrypoints/draft_demo.py`
- Tests: `tests/test_domain_models.py`, `tests/test_review_loop_engine.py`, `tests/test_fake_agent_runners.py`,
  `tests/test_cli_draft_fake_runner.py`, `tests/test_workspace_store.py`, `tests/test_e2e_description_converges.py`
