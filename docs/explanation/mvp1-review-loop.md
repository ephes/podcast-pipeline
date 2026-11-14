# MVP-1 creator/reviewer loop

This explanation documents the MVP-1 creator/reviewer loop as implemented by the review loop orchestrator. It
focuses on the orchestration flow, convergence rules, sticky decisions, and the protocol artifacts written to
workspace storage.

## Roles and orchestration flow

The MVP-1 loop is driven by `run_review_loop_orchestrator`, which coordinates a creator and reviewer runner (often
backed by different models) and then persists the results.

1. Load and preflight the bounded persisted protocol state for this asset and look for existing candidates under `copy/candidates/<asset_id>/candidate_*.json`.
2. If a seed candidate exists, wrap the creator so its first call receives that candidate as
   `CreatorInput.previous_candidate`.
3. Wrap the reviewer so it can enforce locked selections for certain assets.
4. Resume the core loop engine (`run_review_loop_engine`) from completed iterations and its locked decision.
5. Checkpoint each completed round before the next agent call: iteration envelope, candidates/reviews,
   workspace metadata and selected text (if converged), then the protocol state resume marker.

Individual files are replaced atomically. The protocol state is published after the completed round's
artifacts; a later creator/reviewer failure leaves earlier checkpoints resumable. A failed publication
stops the loop before another agent call. Process death between file replacements is not a multi-file
transaction: inspect surviving artifacts when publication itself fails. Terminal resumes leave current
producer selections untouched.

## Convergence rules

The loop engine decides convergence per iteration:

- Converged when the reviewer verdict is `ok` and the creator returns `done=True`.
- If the loop reaches `max_iterations` without convergence, the outcome is `needs_human` with reason
  `iteration_limit`.
- A reviewer verdict of `needs_human` stops immediately with locked reason `reviewer_requested_human`.
- If an existing protocol decision is terminal and has `outcome` locked, the engine returns immediately with no
  new writes, preventing replays from changing history.

## Sticky decisions

Two sticky behaviors prevent regressions once a decision is made:

- Locked protocol decisions: `LoopDecision` locks `outcome`, `final_iteration`, and `reason` when a terminal
  decision is created. When rerunning the loop, `_merge_decision` preserves locked fields from the existing
  decision, so later runs cannot override them.
- Locked selected content: for `slug`, `title_detail`, `title_seo`, and `subtitle_auphonic`, the orchestrator checks
  for an existing selection under `copy/selected/`. If the current candidate differs, it injects a
  `locked_selection` error issue and downgrades an `ok` verdict to `changes_requested`.

## Protocol artifacts

The MVP-1 loop produces protocol artifacts that can be inspected or surfaced by status tooling:

- `copy/protocol/<asset_id>/iteration_XX.json`: per-iteration envelope with creator `done`, the candidate, and the
  reviewer payload.
- `copy/protocol/<asset_id>/state.json`: protocol state snapshot with decision and all iterations; used by
  `src/podcast_pipeline/entrypoints/status.py` to report progress.
- Supporting artifacts written alongside protocol files include:
  - `copy/candidates/<asset_id>/candidate_<uuid>.json` and rendered text formats.
  - `copy/reviews/<asset_id>/iteration_XX.<reviewer>.json`.
  - `copy/selected/<asset_id>.<ext>` when the loop converges.

## References

- Orchestrator: `src/podcast_pipeline/review_loop_orchestrator.py`
- Loop engine: `src/podcast_pipeline/review_loop_engine.py`
- Workspace layout: `src/podcast_pipeline/workspace_store.py`
- Status view: `src/podcast_pipeline/entrypoints/status.py`

## Producer selection fence

The orchestrator captures all selected formats, the selected candidate ID and manual-selection
provenance under the workspace state lock before agent work. Automatic convergence compares
that snapshot inside the final state transaction. A newer pick or custom edit keeps its text,
selected ID and provenance. The completed candidates/reviews are retained, the protocol is locked
as `needs_human` (`selection_conflict`), and the caller receives a selection-changed error.
Reviewing another asset does not conflict. A terminal resume never reselects an old result.

## Immutable review evidence

Each candidate UUID names one complete record. The engine checks this across history before review;
the orchestrator also checks saved candidates, including seeds, and candidate publication rechecks
under the state lock. Identical reuse keeps the existing file. Changed content or metadata requires
a new UUID. Each callback receives copies of prior evidence, and resumed rounds are never rewritten.
Preflight requires contiguous rounds starting at 1, matching candidate asset/review iteration identities,
and a consistent locked terminal decision pointing to the final recorded round. A partial state may
contain only nonterminal completed rounds; malformed state fails before agent calls or artifact writes.
