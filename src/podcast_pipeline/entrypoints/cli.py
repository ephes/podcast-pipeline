from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(add_completion=False)


@app.command()
def version() -> None:
    """Print version."""
    from podcast_pipeline import __version__

    typer.echo(__version__)


@app.command()
def draft(
    *,
    fake_runner: Annotated[
        bool,
        typer.Option(help="Use built-in fake creator/reviewer runners (no Codex/Claude required)."),
    ] = False,
    workspace: Annotated[
        Path | None,
        typer.Option(help="Episode workspace directory (default: create a new ./demo_workspace* directory)."),
    ] = None,
    episode_id: Annotated[str, typer.Option(help="Episode id to write into the workspace.")] = "demo_ep_001",
    asset_id: Annotated[str, typer.Option(help="Asset id to draft (default: description).")] = "description",
    max_iterations: Annotated[int, typer.Option(min=1, help="Maximum loop iterations.")] = 3,
) -> None:
    """Create a draft asset by running the Creator/Reviewer loop."""
    from podcast_pipeline.entrypoints.draft_demo import run_draft_demo

    run_draft_demo(
        fake_runner=fake_runner,
        workspace=workspace,
        episode_id=episode_id,
        asset_id=asset_id,
        max_iterations=max_iterations,
    )


def main() -> None:
    app()
