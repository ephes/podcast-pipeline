# Docs stack decision: Sphinx + MyST

## Status

Accepted (2025-12-19)

## Context

We need a documentation stack that is easy to maintain, supports Markdown out of the box,
keeps configuration minimal, and offers a fast local preview loop.

## Decision

Use Sphinx with MyST Markdown and a minimal `docs/conf.py` configuration.

## Consequences

- Docs live under `docs/` with Diataxis sections.
- Local preview runs via `just docs` (sphinx-autobuild).
- Local HTML builds run via `just docs-build` (sphinx-build).
- If we need theming or search later, we can add a theme or Sphinx extensions.
