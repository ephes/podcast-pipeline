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
- `keywords` (list of strings): comma-separated `<itunes:keywords>` split into terms; empty when the item has none.
  Tag-asset few-shot prompts use these as the published tags.

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

## CMS identifier migration

CMS identifiers hash the canonical API source plus page ID (falling back to link,
then title). The source uses HTTPX URL normalization (including host case and default
ports), removes credentials, query parameters and fragments, and strips trailing
path slashes. Different API paths or origins identify different sources; filters and
requested counts do not change IDs. Repeat exports from one source keep stable IDs.

This replaces the previous page-only digest. Regenerate existing CMS JSONL exports
with `podcast cms-examples` and replace the old files rather than combining old and
new identifiers. Record schema version remains 1.
