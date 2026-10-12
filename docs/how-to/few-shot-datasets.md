# Fetch few-shot datasets

Use these commands to fetch recent episodes and write JSONL files for few-shot selection. For the output schema, see
`reference/few-shot-dataset-jsonl.md`.

## Before you start

- Ensure the `podcast` CLI is available (run `podcast --help`).
- RSS feeds must expose RSS 2.0 `<item>` entries with `<description>` or `<content:encoded>`.
- Wagtail/CMS endpoints must return a list under `items`, `results`, or `pages`.

## RSS feed examples

```bash
podcast rss-examples \
  --feed-url https://example.com/feed.xml \
  --output datasets/rss_examples.jsonl \
  --limit 25
```

Notes:

- `--output` defaults to `datasets/rss_examples.jsonl` if you omit it.
- `--limit` caps how many episodes are written (default `25`).
- `--timeout-seconds` sets the HTTP timeout.
- The JSONL `output` field contains normalized episode description HTML.

## Wagtail/CMS API examples

```bash
podcast cms-examples \
  --api-url https://example.com/api/v2/pages/?type=podcast.EpisodePage \
  --output datasets/cms_examples.jsonl \
  --limit 25
```

Notes:

- The command skips items that do not have a title plus description or shownotes HTML.
- Field names support dot-notation for nested payloads (for example, `meta.html_url`).
- The CLI always sends a `limit` query param; include any other filters in the `--api-url`.

If your Wagtail API returns metadata in `meta`, supply the field mapping explicitly:

```bash
podcast cms-examples \
  --api-url https://example.com/api/v2/pages/?type=podcast.EpisodePage \
  --link-field meta.html_url \
  --slug-field meta.slug \
  --published-field meta.first_published_at \
  --page-id-field meta.id
```

The CMS command supports these field options:

- `--title-field`
- `--summary-field`
- `--description-field`
- `--shownotes-field`
- `--tags-field`
- `--link-field`
- `--slug-field`
- `--published-field`
- `--page-id-field`

## Check the output

Each line in the JSONL file is a single JSON object. See
`reference/few-shot-dataset-jsonl.md` for the full field list.

## Output preservation

Both `podcast rss-examples` and `podcast cms-examples` serialize the complete dataset
before writing a unique temporary file beside the requested output. They flush and
sync that file before atomically replacing the output. Serialization, temporary-file
write, or replacement failures preserve an existing corpus unchanged and remove the
temporary file. Successful exports use UTF-8, sorted JSON keys, and one newline per
record; an empty dataset produces an empty file.

Existing output permission bits are preserved. New exports are owner-only (0600),
matching workspace atomic writes; use `chmod` to grant access when sharing a corpus.
Replacement creates a file owned by the exporting user. An output path that is a
symlink is replaced with a regular file; the symlink target is not modified.
