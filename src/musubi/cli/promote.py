"""`musubi promote` subcommands — operator-driven promotion + rejection.

Both commands wrap existing concept-write endpoints:

- ``musubi promote force <concept-id> --namespace=… --curated-id=…``
  → ``POST /v1/concepts/<concept-id>/promote``
- ``musubi promote reject <concept-id> --namespace=… --reason=…``
  → ``POST /v1/concepts/<concept-id>/reject``

`promote force` requires the operator to have already created the
curated row they're linking to (operators who need the LLM-free
custom-body path should `POST /v1/curated` first, then
pass the resulting id here). Wrapping that into one command is a
clean follow-up; this CLI is the minimum that unblocks the
operator workflows the spec promises.
"""

from __future__ import annotations

import json

import typer

from musubi.cli._client import post_json, resolve_base_url, resolve_token

promote_app = typer.Typer(help="Concept promotion and rejection.", no_args_is_help=True)


@promote_app.command("force")
def force_promote(
    concept_id: str = typer.Argument(..., help="KSUID of the synthesized concept to promote."),
    namespace: str = typer.Option(
        ..., "--namespace", help="Concept's namespace (eg. `nyla/voice/concept`)."
    ),
    curated_id: str = typer.Option(
        ...,
        "--curated-id",
        help=(
            "KSUID of the curated row this concept is being promoted to. "
            "Create the curated row separately via POST /v1/curated "
            "if one doesn't exist yet."
        ),
    ),
    reason: str = typer.Option(
        "operator-force",
        "--reason",
        help="Free-text reason string persisted on the lifecycle event.",
    ),
    api_url: str | None = typer.Option(None, "--api-url", envvar="MUSUBI_API_URL"),
    token: str | None = typer.Option(None, "--token", envvar="MUSUBI_TOKEN"),
) -> None:
    """Force a concept from `matured` to `promoted`, linking it to a curated row.

    Requires an operator-scoped bearer token (`MUSUBI_TOKEN` or `--token`).
    On success, prints the updated concept as JSON.
    """
    base_url = resolve_base_url(api_url)
    bearer = resolve_token(token)
    body = post_json(
        base_url=base_url,
        path=f"/concepts/{concept_id}/promote",
        token=bearer,
        params={"namespace": namespace},
        json_body={"promoted_to": curated_id, "reason": reason},
    )
    typer.echo(json.dumps(body, indent=2))


@promote_app.command("reject")
def reject(
    concept_id: str = typer.Argument(..., help="KSUID of the synthesized concept to reject."),
    namespace: str = typer.Option(..., "--namespace", help="Concept's namespace."),
    reason: str = typer.Option(
        ...,
        "--reason",
        help="Why the promotion was rejected. Persisted on the concept's "
        "`promotion_rejected_reason` field and bumps `promotion_attempts`.",
    ),
    api_url: str | None = typer.Option(None, "--api-url", envvar="MUSUBI_API_URL"),
    token: str | None = typer.Option(None, "--token", envvar="MUSUBI_TOKEN"),
) -> None:
    """Record a promotion rejection (bumps `promotion_attempts`, sets
    `promotion_rejected_{at,reason}`). Three rejections lock the concept
    out of further sweeps — see issue #217 / the promotion gate."""
    base_url = resolve_base_url(api_url)
    bearer = resolve_token(token)
    body = post_json(
        base_url=base_url,
        path=f"/concepts/{concept_id}/reject",
        token=bearer,
        params={"namespace": namespace},
        json_body={"reason": reason},
    )
    typer.echo(json.dumps(body, indent=2))


__all__ = ["promote_app"]
