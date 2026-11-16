# Changelog

## Unreleased

- Bound deterministic draft and fake-demo input reads to coherent regular UTF-8 files, enforce chapter
  byte/line limits before generation, and contain episode-controlled references within the workspace.

- Admit prompt files through coherent bounded regular UTF-8 reads and cap complete runner input at 1 MiB
  before launch. Keep raw agent stderr, decoder details and launch errors out of default diagnostics.

- Require one unambiguous agent JSON object (or one complete json fence), reject duplicate members and
  nonfinite/invalid scalars, and admit structural work before decoding or writing artifacts.

- Bound agent cleanup to a one-second reap allowance, retain primary failures with safe cleanup uncertainty,
  and reject malformed UTF-8 protocol output instead of replacing its bytes.

- Agent calls require complete UTF-8 stdin delivery and retire the owned process group before reaping the leader, including successful calls.

- Recheck the terminal picker's displayed candidate bytes and file generation under the update lock.
  Preserve the prior selection on change and print each commit receipt immediately, with saved/total
  progress on cancellation or later failure.

- Check canonical selected text against candidate state or custom provenance before declaring it ready.
  Escape terminal controls and bound status lines and CLI candidate previews without changing stored text.

- Validate status protocol directory identity and complete review-engine evidence before reporting
  convergence; refuse duplicate JSON members and duplicate, coerced or contradictory review histories.

- Bound read-only status inspection to regular UTF-8 JSON, 8 MiB per file, 32 MiB total and
  10,000 visited entries; report invalid evidence or incomplete inspection without a ready checklist.
  Refused reads count their file size, not the whole per-file allowance; invalid candidate files are named.

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
