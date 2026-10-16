# episode.yaml schema

`episode.yaml` lives in the workspace root and stores per-episode metadata for inputs, audio sources, and agent
configuration.

## Schema sources

- Model: `src/podcast_pipeline/domain/episode_yaml.py`
- JSON schema helper: `src/podcast_pipeline/workspace_schemas.py`

## Example

```yaml
schema_version: 1
episode_id: ep_001
hosts:
  - Jochen
  - Dominik
inputs:
  transcript: /path/to/transcript.txt
  chapters: transcript/chapters.txt
  transcription_audio: /path/to/pp_001_mix.mp3
sources:
  reaper_media_dir: /path/to/ReaperMedia
  tracks_glob: "*.flac"
tracks:
  - track_id: host_01
    path: Host 01.flac
    label: Host 1
    role: host
agents:
  creator:
    command: codex
    args:
      - --format
      - json
    kind: codex
auphonic:
  preset: podcast_pipeline_multitrack
  tracks:
    - id: jochen
      path: /path/to/stems/01-jochen.flac
    - id: dominik
      path: /path/to/stems/02-dominik.flac
      algorithms:
        denoise: true
  metadata:
    title: "Automation & Podcasting"
    subtitle: "Shipping faster episode pipelines"
    summary: "A short summary for feeds and players."
    description: |
      Longer episode description text for Auphonic.
    tags:
      - automation
      - podcasting
    itunes_keywords: "podcast, automation"
```

## Top-level fields

- `schema_version` (int, default `1`)
- `episode_id` (string, required)
- `hosts` (list of strings or null, optional): podcast host names injected into LLM prompts to prevent hallucinated speakers
- `inputs` (object, optional)
- `sources` (object, optional)
- `tracks` (list of objects, optional)
- `agents` (object, optional)
- `auphonic` (object, optional)

## inputs

- `transcript` (string or null): path to a transcript text file.
- `transcript_draft` (string or null): path to the draft transcript text file (if available).
- `transcript_final` (string or null): path to the final transcript text file (if available).
- `chapters` (string or null): path to a chapters text file. For dashboard regeneration, this must be a
  relative path to an existing regular file inside the episode workspace. Absolute paths, `..` components,
  and symlinks escaping the workspace are rejected before the agent starts. Null omits chapter context during regeneration. This containment rule applies to dashboard regeneration;
  the draft, draft-candidates and creator/reviewer input readers retain their existing path and fallback behavior.
- `chapters_draft` (string or null): path to draft chapters text file (if available).
- `chapters_final` (string or null): path to final chapters text file (if available).
- `transcription_audio` (string or null): the single audio file `podcast transcribe` transcribes, typically a master
  mix exported only for this purpose (relative to the workspace is ok). It is never sent to Auphonic.
- Extra keys are allowed and preserved.

## sources

- `reaper_media_dir` (string or null): absolute path to the Reaper media directory.
- `tracks_glob` (string or null): glob for selecting track files.
- Extra keys are allowed and preserved.

## tracks

Each track is an object with:

- `track_id` (string, pattern `[a-z][a-z0-9_]*`)
- `path` (string): path to the audio file, relative to `sources.reaper_media_dir`
- `label` (string or null): display label
- `role` (string or null): optional role tag
- `provenance` (list): provenance entries (see `reference/review-protocol-schemas.md`)
- Extra keys are allowed and preserved.

## agents

Agent configuration mirrors the local CLI config in `reference/configuration.md`. `episode.yaml` overrides the global
config when both are present.

## auphonic

Optional settings used by `podcast produce` (and `--dry-run`) to build an Auphonic payload.

Auphonic receives the individual speaker tracks and mixes them itself (an Auphonic multitrack production). Export one
stem per speaker from Reaper/Ultraschall: FLAC, synced, all the same length, trimmed and edited as in the project. A
master mix is at most an input for transcription (`inputs.transcription_audio`), never for Auphonic.

- `preset` (string): Auphonic preset id or a key in `auphonic.presets` from the global config. It must be an Auphonic
  multitrack preset (see `reference/configuration.md`); `podcast produce` stops before creating a production when
  the preset type does not match.
- `preset_id` (string): Explicit preset id override (skips preset mapping).
- `tracks` (list): the speaker stems, one entry per track:
  - `id` (string, required, unique): the Auphonic track id. It names the track in Auphonic and is the form field name
    of the stem's upload.
  - `path` (string, required): the stem file (relative to the workspace is ok), or an `http(s)://` URL Auphonic
    fetches itself.
  - `algorithms` (object, optional): Auphonic per-track algorithms, passed through as is (for example `denoise`,
    `denoisemethod`, `levelerstrength`, `backforeground`).
  - `offset` (number, optional): seconds by which Auphonic delays this track; finite and nonnegative.

  Without `auphonic.tracks`, the stems are the episode's top-level `tracks` (paths relative to
  `sources.reaper_media_dir`, ids from `track_id`). Tracks with role `mix`, `master`, `final` or `mixdown` are master
  mixes and are skipped. Point ingest at the exported stems, not at untrimmed raw recordings.
- `output_basename` (string, optional): base name of Auphonic's output files (`<output_basename>.mp3`, `.m4a`, ...).
  Defaults to `episode_id`; without it, Auphonic would name a multitrack production's outputs after the first track
  (for example `jochen.mp3`). Must be a plain file name without directories.
- `mode` (string, optional): `multitrack` (default) or `single_track`. `single_track` is an explicit opt-in for
  material that genuinely has one track; it needs `input_file` and a single-track preset.
- `input_file` (string or null): only with `mode: single_track`: the one input file (local path or `http(s)://` URL).
  In a multitrack production it is rejected, so a master mix never reaches Auphonic by accident.
- `input_files` (list or null): only with `mode: single_track`: alternative to `input_file` holding exactly one entry.
- `metadata` (object, optional): Metadata merged into the payload.
- `title`, `subtitle`, `summary`, `description` (string or null): Convenience overrides merged into metadata.
- `tags` (string or list): Tags merged into metadata.
- `itunes_keywords` (string or list): Keywords merged into metadata.
- `chapters` (list): Chapter objects with `title` and optional `start`/`end` (seconds); a plain string is a title
  without times. The payload sends the times to Auphonic as `HH:MM:SS.mmm` strings (for example `4168` becomes
  `"01:09:28.000"`, `12.25` becomes `"00:00:12.250"`), because Auphonic rejects numeric seconds; `--dry-run` shows
  the converted strings.
  Times (including `start_sec`/`end_sec` aliases) must be finite, nonnegative numbers or numeric strings,
  never booleans. Fractional seconds are supported; when both times exist, `end` must be >= `start`.
  Invalid times fail locally before Auphonic credentials are loaded or production is submitted.

If metadata fields are missing, the payload builder falls back to selected copy in `copy/selected/` (for example
`title_detail`, `subtitle_auphonic`, `summary_short`, `description`, `audio_tags`, and `itunes_keywords`).

`podcast transcribe` never reads `auphonic` settings. It transcribes `inputs.transcription_audio`; without it, it falls
back to `tracks` and uses a single preferred mix/master/final track, or the only track. With several stems and no mix
track, set `inputs.transcription_audio` (for example to a master mix exported for transcription).

When the transcriber is `podcast-transcript` (`--command transcribe` or `--command podcast-transcript`), the pipeline
runs it with `TRANSCRIPT_DIR` set to `transcript/<mode>/.podcast-transcript/<hash>/` inside the workspace, where
`<hash>` is the first 16 hex characters of the SHA-256 of the source audio. Episodes whose audio files share a name
(for example two `final_mix.wav` masters) therefore never share podcast-transcript's cache, and changed audio is
transcribed afresh. The cache holds a copy of the audio, so the workspace grows by roughly its size. Only this run's output is imported: if the command writes no transcript, `podcast transcribe`
fails with "Missing transcript output" instead of keeping an older one. A `TRANSCRIPT_DIR` in your environment is
ignored for these runs, and the shared `~/.podcast-transcripts/transcripts/` cache is no longer read (it is left in
place; delete it yourself if you no longer need it). Cache directories for older audio of the same mode are removed
after a successful run. `TRANSCRIPT_HOME` (and its `.env`) is still honored.
