# Changelog

## Unreleased

- Refuse supplemental episode context above 65,536 characters or 200 combined list entries before building asset candidate or review prompts.

- Return committed transcript chunk metadata if old-backup cleanup fails and its warning handler raises an exception.

- Preserve the original transcript chunk publication error when restoring the previous generation fails, and report the restore failure and retained backup path.

- Repair missing text or HTML when retrying an identical immutable review candidate, and report conflicting companion files without changing them.

- Sync the selection directory after removing old formats during a format change or clear, and recover the previous selection if the sync fails.

- Use the public link or title for CMS example identity when the page ID is missing or is neither an integer nor a nonblank string.

- Reject blank saved chapter titles with an indexed configuration error before building CMS chapter text.

- Reject duplicate keys in the global YAML configuration before building an Auphonic payload.

- Report global Auphonic configuration read and UTF-8 decoding errors with the file path.

- Use the published chunk manifest for dashboard counts and flag incomplete fallback inventories.

- Return a validation error when dashboard note edits encounter malformed stored notes, preserving the episode file.

- Apply bounded JSON admission to CMS reads and report unreadable error bodies using the HTTP status.

- Report incomplete CMS audio inventories separately from a confirmed absent ID.

- Bound dashboard selection reads and report unreadable artifacts separately from missing selections.

- Reserve full dashboard job UUIDs so collisions cannot replace retained jobs or bypass conflict checks.

- Bound review candidate evidence reads to regular UTF-8 JSON files and per-file and per-asset byte limits.

- Refuse saved review candidates whose filenames disagree with their UUIDs before replay.

- Support maximum-length workspace filenames during atomic writes and rollback.

- Publish creator iteration records atomically so a failed replacement preserves the previous record.

- Report invalid CMS contributor roles as configuration errors before upload planning.
  An omitted role still defaults to `host`.

- Reject incomplete or conflicting few-shot text pairs with a JSONL line number.
  Legacy `user`/`assistant` pairs and identical dual pairs remain supported.

- Fill remaining few-shot slots in corpus order without re-sorting scores.

- Restore the caller's stderr after dashboard startup failures and shutdown, and keep old jobs'
  stderr out of progress capture after a restart.

- Replay retained dashboard progress when an SSE reconnect cursor is ahead of the current job.

- Report a CMS client error with worker-thread guidance when called from a running asyncio event loop.

- Reject ambiguous or invalid JSON in successful CMS mutation receipts before any follow-up work.

- Apply the pipeline's strict, bounded JSON decoder to the public candidate and review artifact parsers.
- Resolve nested candidate references within the complete drafter response schema.
- Bind prompt provenance references to the full exported receipt, including unused context.
  Existing references still resolve to their original files.
- Reject duplicate prompt template names when constructing the registry.
- Reject all line boundaries in curated tags and preserve the previous selection when
  the normalized tags cannot be read back exactly.
- Refuse commas within individual iTunes keywords when picking or exporting an existing
  selection to django-cast or Auphonic. CMS and audio tags can still contain commas.

- Retire obsolete transcription mode caches after canonical output, YAML and provenance commit.
  Cleanup failures warn and preserve the successful transcription result.

- Sync restored workspace file directories before retiring recovery backups. When rollback cannot
  confirm durability, report the affected files and retain their last-good backups. `podcast transcribe`
  prints the triggering error and every retained backup, including those of nested recovery scopes.

- Auphonic control responses enforce the 8 MiB limit on compressed input and decoded output independently.
- Auphonic control responses with duplicate JSON object members fail before the response is used.
- Auphonic output publication rolls back renames that took effect before an interrupt and retains
  recovery files when it cannot verify ownership.
- Cap the entire staged Auphonic output set at 4 GiB by default, configurable through
  `PODCAST_PIPELINE_DOWNLOAD_TOTAL_MAX_BYTES`. Exceeding the cap preserves the published set.

- Security: the dashboard and `podcast pick --web` require a random per-run session token. The browser receives
  `/?token=...` without a process argument (Python's macOS `osascript` controller, when selected, reads it on
  stdin; otherwise the UI tries a private `0600` launch file that forwards to it). If no browser opens, the UI
  keeps serving and asks you to open the printed URL. The server sets an `HttpOnly`,
  `SameSite=Strict` cookie and forwards to `/`. Requests without the cookie or an `X-Session-Token` header get
  `403`, in the picker for every HTTP method. Responses also send `Referrer-Policy: no-referrer`.

- Security: `AUPHONIC_BASE_URL` must be an `https://` URL with a host and valid port and without credentials,
  `?` or `#`;
  `produce` refuses other values before sending the Auphonic API key or password. An empty value uses the default.

- Security: the `podcast` CLI no longer prints local variables in crash reports, which exposed the Auphonic
  API key or password and the CMS API token; Auphonic credential reprs hide the secrets.

- Security: dashboard and picker responses forbid framing (`X-Frame-Options: DENY`,
  `Content-Security-Policy: frame-ancestors 'none'`) and content sniffing, so other sites cannot clickjack the UIs.

- Update development-only lock entries with known advisories: filelock 4.0.7 -> 4.2.11, virtualenv
  21.14.1 -> 21.15.8 and soupsieve 2.9 -> 4.1.1 (Python 3.11.0-3.11.4 still resolve soupsieve 2.10,
  which keeps some advisories); distlib 0.4.0 -> 0.4.4 and packaging 25.0 -> 26.3 move with them.

- Preserve RSS HTML entity syntax and mixed-XML leading text so quoted markup stays text;
  the shared CMS example normalizer retains entity syntax too.

- Honor declared RSS content, summary and publication tag priorities before document order.

- Retain each RSS mixed-content child tail exactly once during example extraction.

- Remove RSS HTML comments with linear search work, discarding unterminated comment suffixes.

- Tokenize CMS host-link HTML without changing attributes, comments or script/style contents;
  leave ambiguous self-closing anchor/script and other excluded nonvoid elements unchanged.

- Validate CMS chapter clocks (plain ASCII seconds or MM:SS/HH:MM:SS), increasing starts and the TimeField
  day range locally before HTTP.

- Preserve fractional CMS chapter starts at microsecond precision; refuse rounding collisions.

- Disambiguate canonically equivalent Unicode and case-folded Auphonic output names before
  staging, retaining spelling and order with distinct suffixes. Validate publication names
  before moving originals so filename aliases cannot overwrite rollback backups.

- Require complete, nonempty Auphonic downloads: HTTP 200 without Content-Range and with
  identity encoding. Refused partial or empty outputs preserve the previous output set.

- Return committed chunk metadata when old-backup retirement fails; warn with its retained path
  instead of reporting a failed publication or rolling back the accepted generation.
  Ingesting a new transcript retires such stale backups together with the canonical set, so they
  are never mistaken for an interrupted swap of the new source.

- Report chunk recovery, chunking-limit and source-changed-during-staging refusals from `podcast draft`
  as `Error: ...` with exit code 1 instead of a traceback.

- Recover a unique validated retained chunk generation after process death between directory renames,
  before new staging; refuse ambiguous or damaged backups and preserve all recovery evidence.

- Serialize chunk publication, rollback, discovery and transcript invalidation on the stable workspace
  lock; stage outside it and revalidate source before swapping complete generations.

- Bind draft cache reuse to all requested chunker settings and regenerate incompatible summaries
  before candidate generation; unchanged completed generations remain reusable.

- Bound chunk source bytes, tokens, chunk count and aggregate overlap output before retention or writes;
  apply compatible limits to cache discovery and preserve completed chunks on refusal.

- Require exact integer chunker settings before processing; reject booleans, floats and nonfinite values.

- Reject malformed local dashboard/picker Host ports before body reads: require one to five ASCII
  decimal digits in 1..65535, with the existing 403 response instead of parser exceptions.

- Move dashboard storage operations, job admission and SSE snapshots with their context locks into
  the worker pool so slow storage or lock contention cannot block the event loop or shutdown endpoint.

- Bound complete dashboard request-body acquisition to ten seconds; stop on timeout or disconnect
  before starting work or changing workspace state.

- Reject dashboard JSON with duplicate members at any depth or nonfinite numbers before endpoint dispatch.

- Keep control JSON admission linear for unterminated or escape-heavy strings so adversarial agent
  output cannot stall summary or result parsing. Transcriber stdout keeps replacement decoding; only
  agent protocol stdout must be exact UTF-8. Agent and transcription supervision needs `os.waitid`
  (Python 3.13+ on macOS); older interpreters are refused before launch with an explicit message.
  Successful transcription commands retire their process group like agent calls, and a transcription
  timeout reports uncertain cleanup with the same retry guidance.

- Require strict unambiguous summary JSON evidence for deterministic drafts. Limit candidate variants
  to integers from 1 to 10 at CLI and generator admission before rendering or storage.

- Bound deterministic draft and fake-demo input reads to coherent regular UTF-8 files, enforce chapter
  byte/line limits before generation, and contain episode-controlled references within the workspace.

- Admit prompt files through coherent bounded regular UTF-8 reads and cap complete runner input at 1 MiB
  before launch. Keep raw agent stderr, decoder details and launch errors out of default diagnostics.

- Require one unambiguous agent JSON object (or one complete json fence), reject duplicate members and
  nonfinite/invalid scalars, and admit structural work before decoding or writing artifacts.

- Bound agent cleanup to a one-second reap allowance, retain primary failures with safe cleanup uncertainty,
  and reject malformed UTF-8 protocol output instead of replacing its bytes. Recheck unreaped child
  authority before signaling; late feeder errors cannot race a successful result.

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
