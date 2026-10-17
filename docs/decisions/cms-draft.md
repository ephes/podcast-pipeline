# Episode drafts go to django-cast through its editor API

## Status

Accepted (2026-10-16).

## Context

For pp_72 the python-podcast.de draft (page 141, Audio 83) was created with ad-hoc scripts against django-cast's
editor API (DRF token auth). That run showed what the API needs and where it stops:

- `POST api/editor/media/audios/` takes exactly one file per request; there is no endpoint to change an Audio or add a
  file to it, so M4A, Ogg Vorbis and Opus had to be added in the Wagtail admin.
- The page only shows a player when the overview contains an `audio` block for the Audio; `podcast_audio` alone is not
  enough.
- Host profile links belong in the overview text.
- Transcript generation (Voxhelm) can only be started from the Wagtail editor ("Generate transcript"); the editor API
  has no endpoint for it.
- The transcript only got speaker labels after two more manual changes: the Audio's `transcript_diarization_mode` set
  to Enabled (the upload API rejects `enabled`, and `inherit` resolved to off because the empty Wagtail Voxhelm
  settings checkbox overrides `CAST_VOXHELM_DIARIZATION_ENABLED=true`), and the episode's contributors added (Voxhelm
  uses them for the speaker count and the known speakers). The editor API cannot set contributors.

## Decision

- `podcast cms-draft` builds the audio upload and the episode payload from the workspace (selected copy, chapters,
  Auphonic outputs) and the `cms` config, uploads one audio file, and creates the draft with
  `POST api/editor/episodes/`. Re-runs `PATCH` the same draft with `base_revision_id` and `require_unpublished`, found
  through the ids in `cms/draft.json` or `GET api/editor/posts/?parent=&slug=`. The Audio is reused, because the API
  cannot update it; `--new-audio` uploads a new one on request.
- `cms-draft` never publishes. Publishing is the separate `podcast cms-publish`, which asks for confirmation and sends
  the shown revision in `If-Match`.
- The token is only sent to the configured `https://` base URL; redirects are not followed.
- Endpoints django-cast does not offer yet are feature-detected with `OPTIONS` and used as soon as they exist:
  - `PATCH api/editor/media/audios/<id>/` (multipart, one file per request, field `m4a`/`oga`/`opus`) to add the
    further formats to the uploaded Audio;
  - the same `PATCH` with form field `transcript_diarization_mode` to set the mode of an uploaded Audio;
  - `POST api/editor/episodes/<id>/transcript/` to enqueue Voxhelm transcript generation for the episode's audio.

  Two capabilities are detected from the API's answers: `transcript_diarization_mode=enabled` on the upload (a
  file-less upload request is rejected for the mode while unsupported, and for the missing file once supported;
  nothing is created), and an episode `contributors` field `[{"slug": ..., "role": "host"|"guest"}]` (sent in a
  follow-up `PATCH` once episode responses contain `contributors`).
  Until then `cms-draft` ends with the numbered manual steps in the order they must happen: add the further formats,
  set the diarization mode, add the contributors, then "Generate transcript", each with its Wagtail URL. If
  django-cast implements these endpoints under other paths or contracts, only the path constants and the client
  methods in `podcast_pipeline.cms_api` (plus the contributor field shape in `cms_draft.CmsDraftPlan`) change.
- The transcript is only started automatically when diarization is Enabled on the Audio and all hosts are credited
  as contributors; a transcript without speaker labels is never started.

## Consequences

- One command replaces the manual upload, page creation and copy pasting; up to four manual steps in Wagtail remain
  until django-cast adds the endpoints above, and the command lists exactly those that remain.
- Re-running `cms-draft` replaces Wagtail edits of the fields it sends (title, slug, SEO fields, tags, keywords,
  explicit, overview, detail) with the workspace copy.
- Changed Audio metadata (title, subtitle, chapter marks) needs `--new-audio` and leaves the old Audio to be deleted in
  Wagtail.
