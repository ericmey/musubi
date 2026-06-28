"""`musubi context` — retrieve a ranked essence context pack."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from musubi.cli._client import post_json, resolve_base_url, resolve_token

context_app = typer.Typer(
    help="Build ranked context packs from the deployed Musubi API.",
    no_args_is_help=False,
    add_completion=False,
)


@context_app.callback(invoke_without_command=True)
def context(
    namespace: Annotated[
        str,
        typer.Option(
            "--namespace",
            "-n",
            help="Two- or three-segment namespace, e.g. yua/command-chair.",
        ),
    ] = "yua/command-chair",
    query_text: Annotated[
        str,
        typer.Option("--query", "-q", help="Moment/task to align context against."),
    ] = "startup",
    planes: Annotated[
        str,
        typer.Option(
            "--planes",
            help="Comma-separated planes to search, e.g. episodic,curated,concept.",
        ),
    ] = "episodic,curated,concept",
    include_history: Annotated[
        bool,
        typer.Option("--include-history", help="Include superseded/history rows."),
    ] = False,
    max_items: Annotated[int, typer.Option("--max-items", min=1, max=50)] = 8,
    max_chars: Annotated[int, typer.Option("--max-chars", min=120, max=8000)] = 1200,
    candidate_limit: Annotated[int, typer.Option("--candidate-limit", min=1, max=100)] = 30,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON response instead of grouped text."),
    ] = False,
    api_url: Annotated[
        str | None,
        typer.Option("--api-url", envvar="MUSUBI_API_URL", help="Musubi API base URL."),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option("--token", envvar="MUSUBI_TOKEN", help="Operator bearer token."),
    ] = None,
) -> None:
    """Build and print a startup context pack."""

    base_url = resolve_base_url(api_url)
    bearer = resolve_token(token)
    body = {
        "namespace": namespace,
        "query_text": query_text,
        "planes": _parse_planes(planes),
        "include_history": include_history,
        "max_items": max_items,
        "max_chars": max_chars,
        "candidate_limit": candidate_limit,
    }
    response = post_json(base_url=base_url, path="/context", token=bearer, json_body=body)
    if as_json:
        typer.echo(json.dumps(response, indent=2, sort_keys=True))
        return
    typer.echo(_render(response))


def _parse_planes(value: str) -> list[str]:
    planes = [part.strip() for part in value.split(",") if part.strip()]
    return planes or ["episodic"]


def _render(response: dict[str, object]) -> str:
    groups = response.get("groups", [])
    if not isinstance(groups, list) or not groups:
        return "(no context surfaced)"
    lines: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        title = group.get("title")
        if not isinstance(title, str):
            continue
        lines.append(f"{title}:")
        items = group.get("items", [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            evidence = item.get("evidence_handle")
            content = item.get("content")
            if not isinstance(kind, str) or not isinstance(evidence, str):
                continue
            if not isinstance(content, str):
                continue
            lines.append(f"- [{kind}; {evidence}] {content}")
    return "\n".join(lines) if lines else "(no context surfaced)"


def main() -> None:
    context_app()


__all__ = ["context_app", "main"]
