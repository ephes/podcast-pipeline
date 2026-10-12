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

## Selection identity and matching

Examples are unique by a non-blank string `example_id`/`id` (compared exactly, including whitespace),
or by the generated input/output hash. A truthy `example_id` takes precedence over `id`;
if that chosen value is not a non-blank string, the input/output hash is used.
With empty or blank topics, the first record for each id wins in dataset order; later unique
records fill the limit. With topics, positive scores retain their weighted, stable ordering.
Matching uses Unicode words (underscores separate words), casefolding and NFC normalization,
so Latin accented words with precomposable characters and decomposed umlauts match consistently
without single-letter fragments. Combining marks that NFC cannot compose remain word separators.
