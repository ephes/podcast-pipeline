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
inputs:
  transcript: /path/to/transcript.txt
  chapters: /path/to/chapters.txt
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
  preset: podcast_pipeline
  input_file: /path/to/final_mix.wav
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
- `inputs` (object, optional)
- `sources` (object, optional)
- `tracks` (list of objects, optional)
- `agents` (object, optional)
- `auphonic` (object, optional)

## inputs

- `transcript` (string or null): path to a transcript text file.
- `transcript_draft` (string or null): path to the draft transcript text file (if available).
- `transcript_final` (string or null): path to the final transcript text file (if available).
- `chapters` (string or null): path to a chapters text file.
- `chapters_draft` (string or null): path to draft chapters text file (if available).
- `chapters_final` (string or null): path to final chapters text file (if available).
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

Optional settings used by `podcast produce --dry-run` to build an Auphonic payload:

- `preset` (string): Auphonic preset id or a key in `auphonic.presets` from the global config.
- `preset_id` (string): Explicit preset id override (skips preset mapping).
- `input_file` (string or null): Path to the final mix audio file (relative to the workspace is ok).
- `input_files` (list or null): Multiple input paths (preview only).
- `metadata` (object, optional): Metadata merged into the payload.
- `title`, `subtitle`, `summary`, `description` (string or null): Convenience overrides merged into metadata.
- `tags` (string or list): Tags merged into metadata.
- `itunes_keywords` (string or list): Keywords merged into metadata.
- `chapters` (list): Chapter objects with `title` and optional `start`/`end` (seconds).

If metadata fields are missing, the payload builder falls back to selected copy in `copy/selected/` (for example
`title_detail`, `subtitle_auphonic`, `summary_short`, `description`, `audio_tags`, and `itunes_keywords`).
