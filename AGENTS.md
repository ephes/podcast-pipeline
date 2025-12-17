# Agent Instructions

This project uses **bd** (Beads) for issue tracking. Run `bd onboard` to get started.

## Beads DB location (important)

This repo uses a repo-local Beads DB under `.beads/`. To avoid accidentally operating on a different Beads database
(e.g. via a globally-set `BEADS_DIR`), run commands like:

```bash
export BEADS_NO_DAEMON=1
export BEADS_DIR="$PWD/.beads"
bd --no-daemon list
```

## Beads Workflow (Required)

- Use `bd` as the source of truth for work; do not create markdown TODO lists for tracking.
- Copy/paste IDs from `bd ready` / `bd show` (don’t guess ID formats).
- Typical flow: `bd ready` → `bd show <id> --json` → `bd update <id> --status in_progress` → implement → `bd close <id> --reason "Done: <summary>"` → `bd sync`.
- If you discover follow-up work, create a linked issue: `bd create "..." -t task|bug|feature -p 0-4 --deps discovered-from:<id> --json`.
- Keep `.beads/issues.jsonl` in sync with code changes (commit it together).

## Git Commits and Pushes (Required)

- Do **not** run `git commit`, `git push`, or `bd sync` unless the user explicitly asks you to commit/push.
- If the user does not ask for a commit, leave changes uncommitted and report `git status` plus the exact commands the user can run.

## Quality Gates (Required)

A bugfix/feature is not finished unless these pass:

```bash
just lint
just typecheck
just test
```

If `just` shorthands are not available yet, run: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`, `uv run pytest`.

## Note: Creating child issues under epics

Beads hierarchical parents can require `--force` with hyphenated repo prefixes. If `bd create --parent ...` fails with a
prefix mismatch, re-run with `--force`.

## beadsflow (autopilot) workflow (optional)

If you want the same implement→review loop automation as `../llm-node-bare`, this repo can use `beadsflow` with the config in `beadsflow.toml`.

Prereqs:
- The epic has at least one open child task linked as `parent-child` (see `bd show <epic-id>`).
- Comment markers (first non-empty line) drive the state machine:
  - `Ready for review:` (implementer → reviewer)
  - `LGTM` (reviewer → close)
  - `Changes requested:` (reviewer → implement)

Recommended env (direnv-friendly):

```bash
export BEADS_NO_DAEMON=1
export BEADS_DIR="$PWD/.beads"
export BEADSFLOW_CONFIG="$PWD/beadsflow.toml"
```

Safe first runs:

```bash
# Prefer running beadsflow from a local checkout (so bugs can be fixed directly):
uvx --from "$PWD/../beadsflow" beadsflow run <epic-id> --dry-run --verbose
uvx --from "$PWD/../beadsflow" beadsflow run <epic-id> --once --verbose
uvx --from "$PWD/../beadsflow" beadsflow run <epic-id> --interval 30 --verbose

# Convenience wrapper:
just beadsflow run <epic-id> --dry-run --verbose
```


## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
