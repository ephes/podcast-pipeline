# Few-shot dataset JSONL

`podcast rss-examples` and `podcast cms-examples` write JSONL files where each line is a JSON object. The format uses
`version: 1` for the current schema.

## Shared fields

- `version` (int): schema version, currently `1`.
- `source` (string): `rss` or `cms`.
- `example_id` (string): stable id prefixed by the source, for example `rss_<hash>` or `cms_<hash>`.
- `input` (string): prompt input text containing `Title: ...` and optional `Summary: ...`.
- `output` (string): normalized HTML used for few-shot prompting.
- `title` (string): episode title.
- `summary` (string or null): episode summary.
- `link` (string or null): public episode URL.
- `published` (string or null): publication timestamp as provided by the source.

## RSS-only fields

- `description_html` (string): normalized HTML from the RSS `<description>` or `<content:encoded>` tag.
- `guid` (string or null): RSS GUID.
- `feed_url` (string): RSS feed URL used to fetch the data.

## CMS-only fields

- `description_html` (string or null): normalized HTML from the CMS description field.
- `shownotes_html` (string or null): normalized HTML from the CMS shownotes field.
- `tags` (list of strings): tags or categories for the episode.
- `slug` (string or null): page slug.
- `page_id` (string or null): CMS page id.
- `api_url` (string): API URL used to fetch the data.

## Example record

```json
{"api_url":"https://example.com/api/v2/pages/?type=podcast.EpisodePage","description_html":null,"example_id":"cms_ab12cd34ef56","input":"Title: Episode Title\nSummary: Short summary.","link":"https://example.com/episodes/episode-title/","output":"<p>Show notes</p>","page_id":"123","published":"2024-05-01T10:00:00Z","shownotes_html":"<p>Show notes</p>","slug":"episode-title","source":"cms","summary":"Short summary.","tags":["automation","podcasting"],"title":"Episode Title","version":1}
```
