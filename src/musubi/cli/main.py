"""`musubi` CLI entry point.

Thin wrappers over the canonical HTTP API. The CLI never talks to
Qdrant or the planes directly — it posts to `/v1/…` endpoints with
an operator-scoped bearer token.

Environment:

- ``MUSUBI_API_URL`` — base URL including ``/v1`` (default
  ``http://localhost:8100/v1``). Both ``settings.musubi_api_url``
  and the CLI's `--api-url` flag override.
- ``MUSUBI_TOKEN`` — operator-scope JWT. `--token` flag overrides.
"""

from __future__ import annotations

import typer

from musubi.cli.promote import promote_app

app = typer.Typer(
    name="musubi",
    help="Musubi operator CLI — administrative actions over the canonical API.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(promote_app, name="promote", help="Concept promotion / rejection subcommands.")


__all__ = ["app"]
