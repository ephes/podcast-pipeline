# Ingest Reaper media into a workspace

This guide shows how to scan a Reaper media directory and populate `episode.yaml` without copying audio.

## 1. Create a workspace

```bash
podcast init --episode-id ep_001 --workspace ./workspaces/ep_001
```

Notes:

- `--workspace` must not exist; the command creates it.
- The workspace root will contain `episode.yaml` and `state.json`.

## 2. Run ingest

```bash
podcast ingest \
  --workspace ./workspaces/ep_001 \
  --reaper-media-dir /path/to/ReaperMedia \
  --tracks-glob "*.flac"
```

Notes:

- `--reaper-media-dir` must exist and point at the Reaper media folder.
- `--tracks-glob` defaults to `*.flac`.

## 3. Review `episode.yaml`

`podcast ingest` updates `episode.yaml` with the source directory and discovered tracks:

```yaml
episode_id: ep_001
schema_version: 1
sources:
  reaper_media_dir: /absolute/path/to/ReaperMedia
  tracks_glob: "*.flac"
tracks:
  - track_id: ada_01
    path: "Ada 1.flac"
    label: "Ada 1"
```

Details:

- `tracks[].path` is stored relative to `sources.reaper_media_dir`; the audio stays in place.
- `track_id` values are stable for a given path and must match `^[a-z][a-z0-9_]*$`.
- The ingest heuristics convert filenames like `Ada-02.flac` into `track_id: ada_02` and `label: Ada 2`.
- If you re-run ingest, existing `track_id`, `label`, and `role` values for matching paths are preserved.
