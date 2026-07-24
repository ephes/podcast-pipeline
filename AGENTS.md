# Agent Instructions

## Git Commits and Pushes (Required)

- Do **not** run `git commit` or `git push` unless the user explicitly asks you to commit/push.
- If the user does not ask for a commit, leave changes uncommitted and report `git status` plus the exact commands the user can run.

## Quality Gates (Required)

A bugfix/feature is not finished unless these pass:

```bash
just lint
just typecheck
just test
```

If `just` shorthands are not available yet, run: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`, `uv run pytest`.

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **Document remaining work** - Include anything that needs follow-up in the handoff
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update project documentation** - Keep relevant workflow and user-facing docs current
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
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
