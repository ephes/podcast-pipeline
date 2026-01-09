# Podcast Pipeline Documentation

This site follows the Diataxis framework so content is grouped by tutorials, how-to guides,
reference material, and explanations.

## Diataxis sections

- [Tutorials](tutorials/index.md): step-by-step lessons for getting started.
- [How-to guides](how-to/index.md): task-focused guides for specific outcomes.
- [Reference](reference/index.md): technical reference material and APIs.
- [Explanation](explanation/index.md): background material, architecture notes, and rationale.
- [Decisions](decisions/index.md): architecture decisions and rationale.

```{toctree}
:caption: Sections
:maxdepth: 2

tutorials/index
how-to/index
reference/index
explanation/index
decisions/index
```

## Local preview

Run a live preview with:

```
just docs
```

Or build static HTML with:

```
just docs-build
```

The preview runs at http://127.0.0.1:8000 and static output lands in `docs/_build/html/`.
