# Docs stack decision: MkDocs

## Status

Accepted (2025-12-19)

## Context

We need a documentation stack that is easy to maintain, supports Markdown out of the box,
keeps configuration minimal, and offers a fast local preview loop.

## Decision

Use MkDocs with the built-in theme and a minimal `mkdocs.yml` configuration.

## Consequences

- Docs live under `docs/` with Diataxis sections.
- Local preview runs via `uv run mkdocs serve`.
- If we need theming or search later, we can add plugins or a theme such as mkdocs-material.
