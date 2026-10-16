# Configuration

## Agent CLI configuration (local only)

`podcast status` and `podcast review` resolve local CLI settings from either:

1. `episode.yaml` in the workspace root (per-episode overrides)
2. A global config at `~/.config/podcast-pipeline/config.yaml`
   (or the path in `PODCAST_PIPELINE_CONFIG`).

Example configuration:

```yaml
agents:
  creator:
    command: codex
    args:
      - --format
      - json
    kind: codex
    install_hint: https://github.com/openai/codex#install
    check_command: codex --version
  reviewer:
    command: claude
    args:
      - --format
      - json
    kind: claude
```

Notes:

- `command` must be a single executable (no whitespace). Put extra flags in `args`.
- Explicit command paths must be executable regular files; symlinks to such files are supported.
  Bare command names are resolved on PATH. Directories and nonexecutable files fail preflight.
- Missing agent configuration files use defaults. For agent settings, directories, unreadable files
  and invalid UTF-8 produce controlled diagnostics naming the path without exposing file contents.
- `episode.yaml` overrides the global config when both are present.
- This configuration is for local CLI runners only; do not store secrets here.

### Agent CLI timeout

Each agent CLI call (drafter, creator and reviewer) runs with a per-call timeout. When the call exceeds it, the CLI
and any processes it started (its process group) are killed and the step fails with
`<Role> CLI timed out after N s`; a dashboard job then ends as `failed` and the stage accepts a new request. The
timeout is resolved in this order:

1. `podcast draft --timeout <seconds>`. Explicit CLI values must be finite and positive;
   infinity, NaN, zero and negatives fail before workspace changes or agent launch, including dry runs.
   Omit the option to use the next source. The dashboard also uses a positive `/api/draft` body `timeout`;
   omitted, zero and negative body values use the next source. NaN and positive infinity body values fail the job before agent launch.
2. The `PODCAST_PIPELINE_AGENT_TIMEOUT` environment variable (positive seconds).
3. `agents.<role>.timeout_seconds` in `episode.yaml` or the global config (positive seconds).
4. The default of 900 seconds (15 minutes).

Dashboard summarize, regenerate and draft (without a body `timeout`) use sources 2-4. Raise the value if a long
transcript legitimately needs more time per call:

```yaml
agents:
  drafter:
    command: claude
    timeout_seconds: 1800
```

## Auphonic presets (local only)

`podcast produce --dry-run` resolves Auphonic preset ids from the same config file. Define preset keys once and
reference them per episode:

```yaml
auphonic:
  presets:
    podcast_pipeline: "preset-uuid"
```

In `episode.yaml`:

```yaml
auphonic:
  preset: podcast_pipeline
```

Use `preset_id` in `episode.yaml` to skip mapping and set the preset id directly.

Productions are multitrack (Auphonic mixes the speaker stems), so the preset must be an Auphonic **multitrack preset**:
create it in Auphonic's multitrack mode (the API reports `is_multitrack: true`). Single-track presets do not apply to
multitrack productions; `podcast produce` reads the preset before creating a production and stops on a mismatch. Keep a
single-track preset only for episodes that opt into `auphonic.mode: single_track`.

A multitrack preset must not contain speaker tracks. Auphonic appends a preset's `multi_input_files` to the
production's, so a preset created with tracks (for example `jochen`, `dominik`) doubles them and the start fails with
"Multiple tracks with ID ... found". Keep the preset to master settings (output files, loudness, leveler, metadata
defaults; intro/outro entries are fine) and put per-track algorithms into `auphonic.tracks[].algorithms` in
`episode.yaml`. `podcast produce` stops before creating a production when the preset contains speaker tracks.

After changing a preset (for example adding Ogg Vorbis or audiogram outputs), `podcast produce --update` applies it to
the production stored in `state.json` and starts that production again without uploading the stems; re-running a
finished production costs Auphonic credits again. Auphonic re-applies the preset on that update, so the production's
output files become exactly the preset's. The same preset checks run before the update. See "Produce with Auphonic"
in `tutorials/episode-workflow.md`.

## Tag assets (local only)

`cms_tags`, `audio_tags` and `itunes_keywords` follow the show's tag house style: 6-10 short, specific terms
(technologies, projects, libraries, PEPs, Python versions), no show-level constants, no language tags, no host or guest
names and no generic summary phrases. The drafter prompt states these rules, and every generated tag candidate (from
`podcast draft` and dashboard regeneration) then passes a deterministic post-filter that:

- drops tags matching the exclude list (case-insensitive; spaces, hyphens and underscores are equivalent, a leading `#`
  is ignored). Built-in defaults: `podcast`, `python podcast`, `pythonpodcast`, `deutsch`, `german`, `englisch`,
  `english`;
- drops host names from `episode.yaml` `hosts` (`--host`): the full name and any tag containing one of its words of
  three or more letters, so `Dominik Geldmacher` also removes `Dominik`;
- drops entries with more than four words (prose rather than tags) and duplicates. A long line without separators
  stays one entry, so generated prose is dropped instead of being split into single-word tags;
- keeps at most `tags.max` tags (default 12).

Candidates are rewritten in their canonical format (`- tag` bullets for `cms_tags`/`audio_tags`, one comma-separated
line for `itunes_keywords`). Tags you add by hand while picking are not filtered.

When picking (`podcast pick --web` and the dashboard), each tag asset shows one suggestion pool instead of candidate
lists: the union of all its candidates, filtered with the same rules (without the per-candidate cap) and ordered by how
many candidates proposed each tag, ties in first-seen order. `tags.max` also caps the curated selection: saving more
entries is rejected with `400` (`At most N tags can be selected ...`), and so is a tag containing a line break. The default of 12 matches Apple's historical
limit for iTunes keywords. The picking UIs read the `tags` settings without loading `tags.examples`, and fall back to
the defaults when the tag configuration is invalid.

```yaml
tags:
  exclude:            # added to the built-in defaults
    - Programmierung
    - Softwareentwicklung
    - Johannes        # e.g. a regular guest
  max: 12             # hard cap per candidate (positive integer)
  examples: datasets/rss_examples.jsonl
  examples_limit: 5   # past episodes shown per tag prompt (positive integer)
```

`examples` points to a few-shot JSONL dataset from `podcast rss-examples` (uses `itunes:keywords`) or
`podcast cms-examples` (uses `tags`; `cms_tags` prefers CMS tags, the other tag assets prefer keywords). A relative path
resolves against the directory of the file that sets it. For each tag asset the pipeline picks up to `examples_limit`
past episodes whose tags best match the episode topics, applies the same filter to their tags and appends them to the
prompt as few-shot examples. Records without tags are ignored. A missing or invalid dataset fails `podcast draft`
before any agent runs.

The `tags` section can live in the global config and in `episode.yaml`: `exclude` lists are combined, while `max`,
`examples` and `examples_limit` from `episode.yaml` override the global values.

## episode.yaml metadata

`episode.yaml` also stores per-episode inputs, sources, and track metadata. See `reference/episode-yaml.md` for the full
schema and details on `podcast ingest`.

## Concurrent episode edits

Episode YAML updates from the dashboard, ingest, drafting and transcription hold the
workspace lock across reading the latest document and atomically saving changes.
Independent note and track updates therefore survive concurrent operations.
Programmatic read-modify-write callers should use `store.update_episode_yaml()` as
a context manager. For an edit prepared outside the lock, obtain data and its
revision with `read_episode_yaml_with_revision()` and pass `expected_revision` to
`write_episode_yaml()`. A changed or missing document raises `WorkspaceStoreError`;
reload and reapply the edit. Writing without a revision only creates a new file.
Manual editors do not participate in the workspace lock.
