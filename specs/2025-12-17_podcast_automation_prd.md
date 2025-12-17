# PRD: Podcast Production Automation (podcast-pipeline)
Date: 2025-12-17  
Owner: Jochen  
Status: Ready for implementation (MVP-1)

## 1. Problem
The biggest time sink in the current workflow is writing and polishing episode text assets:

- episode description (site + feed + audio file metadata via Auphonic)
- shownotes (link-heavy, ideally grouped by chapters)
- announcements (Mastodon, LinkedIn, YouTube description)
- tags/keywords (CMS tags, audio tags, iTunes keywords)

Audio processing and publishing are downstream; the MVP focuses on “texts on autopilot” with a short human selection step.

## 2. Goals
- Automatically generate **multiple candidate** versions for each required text asset.
- Run an autonomous **Creator/Reviewer loop** (two different models) until both agree the asset is “done”, or stop with “needs human”.
- Support large transcripts via **chunking + hierarchical summarization**.
- Produce **Markdown as canonical** (diff-friendly), plus **deterministic HTML derived** from Markdown for Wagtail RichText copy/paste.
- Keep the pipeline resumable and reproducible via local state files.

## 3. Non-goals (MVP)
- Automatic publishing (publish stays manual after visual inspection).
- Multi-episode parallelism (rate limits / quota / locking later).
- Speaker diarization as a requirement.

## 4. Repo decision
Implementation lives in a standalone repo/tool: `podcast-pipeline` (local-first).

## 5. Episode workspace (“single source of truth”)
Each episode has a workspace directory containing:
- `episode.yaml` (content + configuration; paths, metadata)
- `state.json` (technical IDs + selection state; no secrets)
- `copy/` (candidates, reviews, provenance, selected)
- `transcript/` (draft/final transcript artifacts)
- `auphonic/` (downloads/outputs)

Source audio should not be copied by default. Instead, the manifest references the Reaper media folder:

```yaml
sources:
  reaper_media_dir: "/Users/jochen/Documents/REAPER Media/pp_068"
  tracks_glob: "*.flac"
```

## 6. Required text assets (v0.1)
**Website / feed / audio metadata**
- `description` (Markdown + HTML): used for site detail + list view + feed; also the Auphonic “Summary/Description”.
- `summary_short` (plain text, 2–4 sentences): very short, SEO/preview.
- `title_detail`, `title_seo`, `subtitle_auphonic`, `slug`
- `cms_tags` (list), `audio_tags` (list), `itunes_keywords` (comma-separated string)

**Shownotes**
- `shownotes` (Markdown + HTML): link list with short context, optionally grouped by chapters.

**Announcements (copy only)**
- `mastodon` (1–3 variants)
- `linkedin` (1–3 variants)
- `youtube_description` (typically `description` + chapters appended)

## 7. Text generation approach
### 7.1 Chunking + hierarchical summarization
Transcripts can exceed model context; therefore:
1. Split transcript into chunks (token-based target size, overlap for stability).
2. Generate a structured summary per chunk (topics, facts, links, optional timecodes).
3. Reduce chunk summaries into an episode summary (multi-stage if needed).
4. Generate assets from the episode summary + chapters + metadata.

### 7.2 Creator/Reviewer autopilot
Two roles:
- Creator model generates/updates assets.
- Reviewer model validates against explicit requirements and outputs `ok` or `changes_requested`.

Hard constraint:
- `podcast-pipeline` must not call paid LLM APIs directly or require API keys. It should integrate with existing local
  terminal tools (Codex CLI + Claude Code) by shelling out and capturing machine-readable outputs.

The loop ends when:
- Reviewer verdict is `ok` AND Creator sets `done=true`.

All feedback is stored as JSON under `copy/reviews/<asset>/iteration_XX.*.json`.

## 8. RichText in Wagtail
Experiment result (2025-12-17):
- Copy/pasting HTML RichText (e.g. `<h2>`, `<p>`, `<ul>`, `<a href=...>`) into a Wagtail RichText field worked and rendered correctly.

Therefore:
- Markdown is canonical.
- HTML is derived deterministically from Markdown and used for copy/paste into Wagtail.

## 9. Reliability
- Resumable steps: every step writes state/provenance incrementally.
- Retry/backoff for external integrations where relevant (Auphonic API if enabled, link checks). For local CLI runners,
  use process-level retries with clear error reporting.
- Global `--dry-run` should be supported for request/payload preview without API calls.

## 10. Milestones
- M1: Chunking + summaries skeleton + asset generators (no real LLM API calls required; stubs acceptable).
- M2: Creator/Reviewer loop with JSON protocol + convergence detection.
- M3: Interactive selection (`podcast pick`) producing `copy/selected/*`.
- M4: Draft + final transcription integration (`podcast-transcript`).
- M5: Auphonic preset/template + field mapping; optional API integration later.
