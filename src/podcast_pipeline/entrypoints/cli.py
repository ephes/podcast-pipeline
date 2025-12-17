import typer

app = typer.Typer(add_completion=False)


@app.command()
def version() -> None:
    """Print version."""
    from podcast_pipeline import __version__

    typer.echo(__version__)


def main() -> None:
    app()
