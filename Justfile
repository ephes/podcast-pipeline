set dotenv-load := true

default:
  @just --list

# Install/sync the project environment (includes dev group by default).
sync *ARGS="":
  uv sync {{ARGS}}

# Docs
docs:
  uv sync
  uv run mkdocs serve

docs-build:
  uv sync
  uv run mkdocs build

docs-clean:
  rm -rf site

# Quality gates
lint *ARGS="":
  if uv sync; then uv run ruff check . && uv run ruff format --check .; elif [ -x .venv/bin/ruff ]; then .venv/bin/ruff check . && .venv/bin/ruff format --check .; else echo "uv sync failed and .venv/bin/ruff is missing" >&2; exit 1; fi

fmt:
  uv sync
  uv run ruff format .

typecheck *ARGS="":
  if uv sync; then uv run mypy src tests {{ARGS}}; elif [ -x .venv/bin/python ]; then .venv/bin/python -m mypy src tests {{ARGS}}; else echo "uv sync failed and .venv/bin/python is missing" >&2; exit 1; fi

test *ARGS="":
  if uv sync; then uv run pytest {{ARGS}}; elif [ -x .venv/bin/python ]; then .venv/bin/python -m pytest {{ARGS}}; else echo "uv sync failed and .venv/bin/python is missing" >&2; exit 1; fi

pre-commit:
  uv sync
  uv run pre-commit run -a

# Beads helper: `just bead` -> `bd ready`, `just bead <cmd> ...` -> `bd <cmd> ...`
bead *args:
  @if [ -z "{{args}}" ]; then BEADS_NO_DAEMON=1 BEADS_DIR="$PWD/.beads" bd --no-daemon ready; else BEADS_NO_DAEMON=1 BEADS_DIR="$PWD/.beads" bd --no-daemon {{args}}; fi

# beadsflow helper: run beadsflow from sibling repo checkout.
# Usage: `just beadsflow run <epic-id> --dry-run --verbose`
beadsflow *args:
  @if [ ! -d "$PWD/../beadsflow" ]; then echo "Missing ../beadsflow checkout; clone it next to this repo to run beadsflow locally." >&2; exit 1; fi
  @BEADS_NO_DAEMON=1 BEADS_DIR="$PWD/.beads" BD_ISSUE_PREFIX=podcast-pipeline BEADSFLOW_CONFIG="$PWD/beadsflow.toml" uvx --from "$PWD/../beadsflow" beadsflow {{args}}

# Loop helper: run beadsflow for an epic (defaults to --once --verbose).
# Usage: `just loop <epic-id> [--dry-run|--once|--verbose]`
loop epic *args="":
  @if [ -z "{{epic}}" ]; then echo "Usage: just loop <epic-id> [--dry-run|--once|--verbose]" >&2; exit 1; fi
  @if [ -z "{{args}}" ]; then just beadsflow run {{epic}} --once --verbose; else just beadsflow run {{epic}} {{args}}; fi
