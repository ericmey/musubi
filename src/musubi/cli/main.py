"""`musubi` CLI entry point.

Thin wrappers over the canonical HTTP API. Almost every command posts to `/v1/…`
endpoints with an operator-scoped bearer token and never touches storage directly.

**The one deliberate exception is ``musubi validate rows``.** It connects to Qdrant
directly, because it must be able to read rows the API *cannot serve* — a row whose
payload the canonical model rejects 500s on every `/v1` read, and those are exactly the
rows an integrity sweep exists to find. Going through the API would mean the instrument
is blind to precisely the thing it is looking for. It is read-only (raw ``scroll``, no
writes, and no code path that mutates).

That exception has an operational consequence, so it is stated rather than left implied:

- ``validate rows --api-key`` puts the Qdrant key in **argv**, which is visible in the
  process list (``ps``) and in shell history. Prefer running it on the Musubi host where
  Qdrant is not exposed and no key is needed; if a key is required, pass it via the
  environment rather than the flag.
- Qdrant is not exposed off-host, so this command is an **on-host operator tool**, not
  something a laptop can point at production.

Environment:

- ``MUSUBI_API_URL`` — base URL including ``/v1`` (default
  ``http://localhost:8100/v1``). The ``--api-url`` flag overrides.
  Env resolution happens in Typer at the command boundary — this
  module doesn't consult ``musubi.config.get_settings()``.
- ``MUSUBI_TOKEN`` — operator-scope JWT. ``--token`` flag overrides.
"""

from __future__ import annotations

import typer

from musubi.cli.context import context_app
from musubi.cli.promote import promote_app
from musubi.cli.validate import validate_app

app = typer.Typer(
    name="musubi",
    help="Musubi operator CLI — administrative actions over the canonical API.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(context_app, name="context", help="Build ranked startup context packs.")
app.add_typer(promote_app, name="promote", help="Concept promotion / rejection subcommands.")
app.add_typer(
    validate_app,
    name="validate",
    help="Non-mutating integrity sweeps — find rows their own model can no longer read.",
)


__all__ = ["app"]
