from __future__ import annotations

from pathlib import Path

import typer

from podcast_pipeline.rss_examples import (
    RssExamplesError,
    fetch_rss_examples,
    safe_rss_source,
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
        written = write_rss_examples_jsonl(examples=examples, output_path=output)
    except RssExamplesError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"RSS feed: {safe_rss_source(feed_url)}")
    typer.echo(f"Episodes: {written}")
    typer.echo(f"Output: {output.expanduser().resolve()}")
