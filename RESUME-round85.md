# Round 85 resume note

Branch: `fix/r85-candidate-status`, based on `origin/main` at
`8a292ab15f5e894b5c4926d5c3f7517b94e146d0`.
Remote verified: `git@github.com:ephes/podcast-pipeline.git`.
All four report defects were re-verified against that default-branch snapshot; none was already fixed.

## Stop reason

The required Claude review command used the installed `claude-review-loop` harness with
`--model claude-opus-5-5 --effort high`. It returned `PROVIDER_ERROR` before reviewing any code:
“Failed to authenticate: OAuth session expired and could not be refreshed”.
No review verdict exists. The owner explicitly requires parking the branch and stopping on this condition.
Nothing was merged. No PR, deployment, release, tag, or Actions run was requested or used.

The initial harness attempt could not write its default lock cache; the actual authentication-failed
attempt used workspace-local lock and ledger directories. Local artifacts are in the workspace's
`.cache/b10-review-2/`, outside this repository. Reauthenticate outside this session, then run a fresh review.

## B10 implemented, unreviewed

1. `pipeline-candidate-artifact-set-rollback`: serialize/render every representation first; use the
   existing `preserve_files` helper under the workspace state lock; publish discovery JSON last.
   Fault-injected render and post-replace failures cover create/rewrite and exact prior bytes/absence.
2. `pipeline-auphonic-status-integer-error-contract`: accept numeric codes 0–999 and strings of
   one to three ASCII digits; malformed numeric evidence and booleans raise a safe `AuphonicApiError`.
   Unknown admitted codes remain running even with a Done label. Polling and cleanup tests forbid mutations.

README documents the contracts and handled-error/process-death boundary. No changelog exists.
The new defect regressions were run against the original sources: 13 candidate/polling failures and
8 cleanup failures. The supporting unknown-code tests also pass on the original source.

Validation on the fixed tree:

- `just lint`: passed (Ruff check and format).
- `just typecheck`: passed (mypy, 118 source files).
- Focused workspace-store/Auphonic API tests: passed.
- `just test`: 1,113 passed, 23 errors, 3 deprecation warnings. Every error is a
  `tests/test_dashboard_browser.py` fixture failing at `playwright.chromium.launch()`.
  macOS sandbox rejects `bootstrap_check_in ... MachPortRendezvousServer` with
  `Permission denied (1100)`. Browser coverage has not run and must pass outside this sandbox.
  Do not replace it with Actions or bypass it to merge.

## B11 not implemented

Read the full slice descriptions and validation notes in
`~/workspaces/ws-work-planning/reviews/scout-round85.md`, sections 30–31, and re-verify against
current `origin/main` before proceeding, in this order:

1. `pipeline-auphonic-control-response-admission`: stream sync/async control response bodies under
   a documented decoded byte ceiling; safely reject encoding/depth/integer errors; include empty
   successful DELETE; retain origin credential policy and recorded created UUID on later failures.
   Synthetic HTTPX streaming tests must prove closure, bounded rejection and no repeat paid create/start.
2. `pipeline-auphonic-preset-proof-before-mutation`: define/validate preset mapping, boolean mode,
   and track schema, documenting supported legacy defaults. Reject ambiguous evidence before create,
   update callbacks, upload or start. Test creation and rerun with malformed and accepted legacy fixtures,
   including preservation of downloaded flags.

Keep B10 then B11 serial where they share the helper/docs. Use synthetic fixtures and local checks only.
Run a clean/suggestion-only Opus review before merging either bundle. Fetch immediately before each
merge; rebase if the default branch moved and rerun checks. Merge with `--no-ff`, run the full local
checks after merging and before pushing only the default branch, then verify via `git ls-remote`.
No Actions, feature pushes except parking, PRs, deploys, releases/tags, host access or work-app writes.
