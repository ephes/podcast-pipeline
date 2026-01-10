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
