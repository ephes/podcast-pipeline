# Auphonic gets the speaker stems, not a master mix

## Status

Accepted (2026-10-16). Supersedes the earlier "a production takes exactly one input file; multitrack productions are
not supported" stance of `podcast produce`.

## Context

`podcast produce` used to build a single-track Auphonic production. The documented flow told producers to export a
master mix from Reaper/Ultraschall and use it both for transcription and as `auphonic.input_file`; more than one input
file was rejected.

That is the wrong split of work. Auphonic's multitrack algorithms (per-track leveling, noise and hum reduction,
crossgate against mic bleed, ducking, loudness on the final mix) need the individual speaker tracks. A file we mixed
ourselves has already lost that information, so Auphonic can only treat it as one voice. While producing pp_72 a
pre-mixed FLAC almost went to Auphonic before the producer stopped it.

## Decision

- Auphonic productions are **multitrack** by default. Producers export one stem per speaker from Reaper/Ultraschall
  (FLAC, synced, all the same length, trimmed and edited as in the project) and `podcast produce` uploads them as an
  [Auphonic multitrack production](https://auphonic.com/help/api/multitrack.html): `is_multitrack: true`, one
  `multi_input_files` entry `{type: "multitrack", id: <track id>}` per stem (optionally with per-track `algorithms`
  and `offset`), a multipart upload to `production/{uuid}/upload.json` with the track id as the form field name, then
  `production/{uuid}/start.json`. Auphonic does the mixing.
- The stems come from `auphonic.tracks` in `episode.yaml` (explicit `id` + `path` per stem), or, when that is absent,
  from the ingested `tracks` (tracks with a `mix`/`master`/`final`/`mixdown` role are skipped).
- A master mix is at most an input for transcription. It lives in `inputs.transcription_audio`, and
  `podcast transcribe` no longer reads any Auphonic setting. `auphonic.input_file` in a multitrack production is a
  configuration error.
- A single-file production remains only as an explicit opt-in (`auphonic.mode: single_track` with
  `auphonic.input_file`), for material that genuinely has one track.
- The configured preset must be a **multitrack preset** (created in Auphonic's multitrack mode, `is_multitrack: true`).
  Before creating a production, `podcast produce` reads the preset and refuses a type mismatch, so no production is
  created with a preset that does not apply. The preset must not contain speaker tracks either: Auphonic appends a
  preset's `multi_input_files` to the production's, which duplicated every track id on the first real run (pp_72).
  Per-track settings live in `auphonic.tracks[].algorithms`; `podcast produce` refuses a preset with speaker tracks.
- The production sets `output_basename` (default: `episode_id`, override: `auphonic.output_basename`). Without it
  Auphonic names a multitrack production's outputs after the first track (`jochen.mp3` on the pp_72 run).
- The existing safety properties stay: the production UUID is stored in `state.json` before the paid start request,
  reruns resume instead of paying twice, `--restart` starts a new production on purpose, a lock file keeps one run per
  workspace, credentials go only to the API origin, and `--dry-run` prints the full payload (including the track list)
  without API calls. A stored production of the other type (for example a pre-mixed single-track production from
  before this change) is not resumed; `--restart` replaces it.

## Consequences

- Producers export stems instead of (or in addition to) a master mix. A master mix is still useful for transcription.
- Existing `episode.yaml` files with `auphonic.input_file` fail `podcast produce` with a message that explains the
  migration: list the stems in `auphonic.tracks`, move the mix to `inputs.transcription_audio`.
- A multitrack preset has to exist in the Auphonic account; single-track presets (for example the mono "Python
  Podcast" preset) cannot be reused for multitrack productions.
- Uploads are larger (one file per speaker) and go out in one multipart request. To fix settings (preset outputs, metadata, chapters,
  per-track algorithms) without uploading them again, `podcast produce --update` edits the stored production and
  starts it again. It never sends `reset_data` (which deletes a multitrack production's tracks and stems) or
  `multi_input_files` (which an update appends as extra tracks); per-track settings go to each track's own endpoint.
