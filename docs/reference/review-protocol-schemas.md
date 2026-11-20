# Creator/Reviewer JSON Schemas

This reference describes the JSON payloads written by the creator and reviewer steps in the review loop.

## Schema source and file locations

- Schema helpers: `src/podcast_pipeline/protocol_schemas.py` (backed by models in `src/podcast_pipeline/domain/models.py`).
- Creator candidate files: `copy/candidates/<asset_id>/candidate_<uuid>.json`.
- Reviewer iteration files: `copy/reviews/<asset_id>/iteration_XX.<reviewer>.json` (or `iteration_XX.json` if the
  reviewer name is omitted).

`parse_candidate_json` and `parse_review_iteration_json` accept JSON text, UTF-8 bytes, or a UTF-8
bytearray. They apply the pipeline's control JSON limits (8 MiB, 64 levels, 100,000 tokens and
256 characters per numeric token) before model validation. Invalid UTF-8, malformed JSON, duplicate
object members at any depth and nonfinite numbers raise a content-free `ValueError`.

## Creator candidate (Candidate)

Required fields:

- `asset_id` (string, pattern `[a-z][a-z0-9_]*`)
- `content` (string)

Optional or filled by the system:

- `candidate_id` (UUID)
- `format` (`markdown` | `plain` | `html`, default `markdown`)
- `created_at` (ISO 8601 timestamp)
- `provenance` (list of provenance entries)

Provenance entry fields:

- Required: `kind`, `ref`
- Optional: `created_at`, `metadata`

## Reviewer iteration (ReviewIteration)

Required fields:

- `iteration` (integer >= 1)
- `verdict` (`ok` | `changes_requested` | `needs_human`)

Optional or filled by the system:

- `issues` (list of issue objects)
- `reviewer` (string or null)
- `created_at` (ISO 8601 timestamp)
- `summary` (string or null)
- `provenance` (list of provenance entries)

Issue entry fields:

- Required: `message`
- Optional: `issue_id` (UUID), `severity` (`error` | `warning`), `code`, `field`

Constraints:

- `verdict=ok` cannot include issues with `severity=error`.

## Drafter alternatives

Drafting and dashboard regeneration allocate fresh candidate UUIDs in the application, ignoring any
`candidate_id` returned by the drafter. Regeneration appends distinct alternatives and preserves existing
candidates, selections and reviews. The complete candidate response is validated before it is written.

`asset_candidates_response_json_schema` describes the object containing the `candidates` array.
References inside each candidate resolve against `$defs` at the root of that response schema.
The response object forbids extra fields and `num_candidates`, when supplied, fixes the array length.

## Completed-round checkpoints

`copy/protocol/<asset_id>/state.json` (version 1) records `asset_id`, `max_iterations`,
all completed iteration envelopes and an optional locked `decision`. Each envelope contains
`version`, `iteration`, `creator` (`done`, `candidate`) and `reviewer` (ReviewIteration).
The orchestrator publishes this resume marker after the round's supporting artifacts and
passes it back to the engine on retry. Unknown envelope fields and malformed models are refused;
no incomplete creator-only round is advertised. Persisted candidates and reviews must include all
model fields; resume never invents missing UUIDs or timestamps. Candidate artifacts must still match
the checkpoint records. Keep the original maximum when resuming.

Candidate UUIDs bind the complete candidate record (asset, format, content, timestamp and provenance).
Review creators must return a fresh UUID for any changed record. Reusing an identical candidate is allowed;
its saved candidate artifacts are left in place. Creator/reviewer inputs are detached copies so a callback
cannot mutate earlier evidence.

Resume accepts at most 8 MiB of protocol JSON and refuses duplicate JSON members, decoder failures,
unknown envelope fields, non-contiguous/reversed rounds, foreign candidates, mismatched review numbers,
changed records under one UUID and inconsistent terminal references. Validation also applies to direct
`LoopProtocolState` engine inputs before any callback. Terminal decisions refer to the last completed round
and lock the outcome. Invalid evidence raises `LoopProtocolError` (a `ValueError`) with a content-free message.
Back up and repair the evidence explicitly rather than resetting over it.
