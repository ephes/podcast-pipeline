# Creator/Reviewer JSON Schemas

This reference describes the JSON payloads written by the creator and reviewer steps in the review loop.

## Schema source and file locations

- Schema helpers: `src/podcast_pipeline/protocol_schemas.py` (backed by models in `src/podcast_pipeline/domain/models.py`).
- Creator candidate files: `copy/candidates/<asset_id>/candidate_<uuid>.json`.
- Reviewer iteration files: `copy/reviews/<asset_id>/iteration_XX.<reviewer>.json` (or `iteration_XX.json` if the
  reviewer name is omitted).

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
