# Changelog

## Unreleased

- Bound read-only status inspection to regular UTF-8 JSON, 8 MiB per file, 32 MiB total and
  10,000 visited entries; report invalid evidence or incomplete inspection without a ready checklist.

- Bind regeneration chapter reads to opened workspace/file authority with coherent UTF-8 byte/line
  limits; refuse swaps, special files and excessive chapter input before starting the agent.
- Cap chunk source and complete chunk/reduction prompts at 256 KiB of UTF-8. Admit reduction JSON
  incrementally and refuse excessive context without truncation or publication for that call.

- Admit few-shot corpora through a bounded nonblocking regular-file read with generation checks.
  Refuse duplicate JSON members, nonstandard/nonfinite numbers and excessive nesting/integers with
  content-free line diagnostics before tag generation starts.

- Bind RSS example IDs to canonical credential-free feed sources; regenerate old corpora to avoid mixed IDs.
  Atomically publish validated chunk summary JSON so pre-publication failures preserve the old cache.

- Bound RSS XML elements, depth and text during incremental item extraction; refuse DTDs and preserve
  prior exports on admission failure. Remove source credentials, query and fragment from RSS corpora and CLI output.

- Bound optional refinement summary reads to coherent regular UTF-8 files of at most 1 MiB; omit unusable
  context with a safe diagnostic. Refuse complete refinement prompts over 256 KiB before starting the agent,
  preserving existing candidates and selections; the pick UI reports this refusal as an input error (400).
- Own generated candidate IDs, observation dates and drafter provenance in the application. Validate generated
  titles, subtitles and slugs before publishing a response; store short copy in its natural plain-text format.
- Refuse stored candidates and reviews whose embedded identity differs from the requested path, preserving
  their bytes for repair. Restore candidate artifacts, affected selections and state together after handled
  dashboard deletion failures.
