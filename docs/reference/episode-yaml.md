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
```

## Top-level fields

- `schema_version` (int, default `1`)
- `episode_id` (string, required)
- `inputs` (object, optional)
- `sources` (object, optional)
- `tracks` (list of objects, optional)
- `agents` (object, optional)

## inputs

- `transcript` (string or null): path to a transcript text file.
- `chapters` (string or null): path to a chapters text file.
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
