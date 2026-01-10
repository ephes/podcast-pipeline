from __future__ import annotations

from pathlib import Path

import typer

from podcast_pipeline.rss_examples import (
    RssExamplesError,
    fetch_rss_examples,
    write_rss_examples_jsonl,
)


def run_rss_examples(
    *,
    feed_url: str,
    output: Path,
    limit: int,
    timeout_seconds: float,
) -> None:
    if not feed_url.strip():
        raise typer.BadParameter("feed_url must be non-empty")
    if limit < 1:
        raise typer.BadParameter("limit must be >= 1")
    if timeout_seconds <= 0:
        raise typer.BadParameter("timeout_seconds must be > 0")

    try:
        examples = fetch_rss_examples(
            feed_url=feed_url,
            limit=limit,
            timeout_seconds=timeout_seconds,
        )
    except RssExamplesError as exc:
        raise typer.BadParameter(str(exc)) from exc

    write_rss_examples_jsonl(examples=examples, output_path=output)
    typer.echo(f"RSS feed: {feed_url}")
    typer.echo(f"Episodes: {len(examples)}")
    typer.echo(f"Output: {output.expanduser().resolve()}")
