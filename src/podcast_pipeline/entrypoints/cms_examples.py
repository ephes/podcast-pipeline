from __future__ import annotations

from pathlib import Path

import typer

from podcast_pipeline.cms_examples import (
    CmsExamplesError,
    CmsFieldMapping,
    fetch_cms_examples,
    write_cms_examples_jsonl,
)


def run_cms_examples(
    *,
    api_url: str,
    output: Path,
    limit: int,
    timeout_seconds: float,
    title_field: str,
    summary_field: str,
    description_field: str,
    shownotes_field: str,
    tags_field: str,
    link_field: str,
    slug_field: str,
    published_field: str,
    page_id_field: str,
) -> None:
    if not api_url.strip():
        raise typer.BadParameter("api_url must be non-empty")
    if limit < 1:
        raise typer.BadParameter("limit must be >= 1")
    if timeout_seconds <= 0:
        raise typer.BadParameter("timeout_seconds must be > 0")

    title = title_field.strip()
    if not title:
        raise typer.BadParameter("title_field must be non-empty")

    def normalize_optional(value: str) -> str | None:
        text = value.strip()
        return text or None

    fields = CmsFieldMapping(
        title=title,
        summary=normalize_optional(summary_field),
        description=normalize_optional(description_field),
        shownotes=normalize_optional(shownotes_field),
        tags=normalize_optional(tags_field),
        link=normalize_optional(link_field),
        slug=normalize_optional(slug_field),
        published=normalize_optional(published_field),
        page_id=normalize_optional(page_id_field),
    )

    try:
        examples = fetch_cms_examples(
            api_url=api_url,
            limit=limit,
            timeout_seconds=timeout_seconds,
            fields=fields,
        )
    except CmsExamplesError as exc:
        raise typer.BadParameter(str(exc)) from exc

    write_cms_examples_jsonl(examples=examples, output_path=output)
    typer.echo(f"CMS API: {api_url}")
    typer.echo(f"Episodes: {len(examples)}")
    typer.echo(f"Output: {output.expanduser().resolve()}")
