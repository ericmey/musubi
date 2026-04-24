"""Shared HTTP helpers for CLI subcommands.

Keeps subcommand files thin — every CLI call goes through
:func:`resolve_base_url` + :func:`resolve_token` + :func:`post_json`
so credential discovery + error surfacing are consistent.

Env-var resolution is delegated to Typer (`typer.Option(envvar=...)`)
at the command boundary, so this module never reads env vars
directly — the project guardrail confines env reads to
``musubi.config`` / ``musubi.settings``.
"""

from __future__ import annotations

from typing import Any

import httpx
import typer

_DEFAULT_API_URL = "http://localhost:8100/v1"


def resolve_base_url(value: str | None) -> str:
    """Return the API base URL.

    ``value`` is whatever Typer handed us after resolving
    ``--api-url`` + ``MUSUBI_API_URL``; we only layer the localhost
    default and strip trailing slashes so subcommands can concatenate
    paths without worrying about `//`.
    """
    if value:
        return value.rstrip("/")
    return _DEFAULT_API_URL


def resolve_token(value: str | None) -> str:
    """Return the bearer token.

    ``value`` is whatever Typer handed us after resolving
    ``--token`` + ``MUSUBI_TOKEN``. No default — operator actions
    require an explicit token, and silently sending unauthenticated
    requests would surface as 401 with a less-helpful error.
    """
    if value:
        return value
    typer.echo(
        "error: no operator token configured. Set MUSUBI_TOKEN or pass --token.",
        err=True,
    )
    raise typer.Exit(code=2)


def post_json(
    *,
    base_url: str,
    path: str,
    token: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """POST to `{base_url}{path}`; return the decoded JSON body.

    On non-2xx: prints the server's error body + status to stderr
    and exits with code 1. Network errors exit with code 3 so
    operator scripts can distinguish infra from semantic failures.
    """
    url = f"{base_url}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.post(url, params=params, json=json_body, headers=headers, timeout=timeout_s)
    except httpx.HTTPError as exc:
        typer.echo(f"error: request to {url} failed: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    if resp.status_code >= 400:
        typer.echo(f"error: {resp.status_code} {resp.reason_phrase}: {resp.text}", err=True)
        raise typer.Exit(code=1)
    return dict(resp.json())


__all__ = ["post_json", "resolve_base_url", "resolve_token"]
