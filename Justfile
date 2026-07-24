set dotenv-load := true

default:
  @just --list

# Install/sync the project environment (includes dev group by default).
sync *ARGS="":
  uv sync {{ARGS}}

# Docs
docs port="8000":
  uv sync
  uv run sphinx-autobuild docs docs/_build/html --port {{port}}

docs-build:
  uv sync
  uv run sphinx-build -b html docs docs/_build/html

docs-clean:
  rm -rf docs/_build

# Quality gates
lint *ARGS="":
  if uv sync; then uv run ruff check . && uv run ruff format --check .; elif [ -x .venv/bin/ruff ]; then .venv/bin/ruff check . && .venv/bin/ruff format --check .; else echo "uv sync failed and .venv/bin/ruff is missing" >&2; exit 1; fi

fmt:
  uv sync
  uv run ruff format .

typecheck *ARGS="":
  if uv sync; then uv run mypy src tests {{ARGS}}; elif [ -x .venv/bin/python ]; then .venv/bin/python -m mypy src tests {{ARGS}}; else echo "uv sync failed and .venv/bin/python is missing" >&2; exit 1; fi

check: lint typecheck test

test *ARGS="":
  if uv sync; then uv run pytest {{ARGS}}; elif [ -x .venv/bin/python ]; then .venv/bin/python -m pytest {{ARGS}}; else echo "uv sync failed and .venv/bin/python is missing" >&2; exit 1; fi

pre-commit:
  uv sync
  uv run pre-commit run -a

# Dashboard helper: `just dashboard ep_068` -> `--workspace workspaces/ep_068`.
dashboard workspace:
  uv sync
  @if [ -d "{{workspace}}" ]; then uv run podcast dashboard --workspace "{{workspace}}"; else uv run podcast dashboard --workspace "workspaces/{{workspace}}"; fi
