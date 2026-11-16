# Changelog

## Unreleased

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
