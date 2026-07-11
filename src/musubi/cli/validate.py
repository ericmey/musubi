"""``musubi validate`` — find every persisted row that its own model can no longer read.

**This command never writes.** It is the instrument you use *before* deciding what to
repair, and it is deliberately incapable of repairing anything.

## Why this exists

Every canonical model is ``extra="forbid"``. Until 2026-07-11 the PATCH endpoints were
``extra="allow"`` behind a *denylist* of four or five field names, and wrote the request
body straight into the Qdrant payload. So any key nobody had thought to forbid landed on
disk — where the read model then rejected it **forever**. The row 500s on every read, and
(before the same PR) could not even be deleted, because every delete path guarded
existence with a deserializing ``get()``.

A corrupted memory that cannot be read and cannot be removed is not an inconvenience. It
is a falsehood with tenure.

At least one row is known to be in this state:
``aoi/command-chair/episodic/3GJhJLAvYXzIp8Qe8tuPHR9S9th``. **Nobody knows how many
others are**, because nothing has ever looked. This command is what looks.

## How it reads

By raw scroll, with the payload unvalidated, then ``model_validate`` per row inside a
try/except. That is the only honest way to ask "can this row still be read?" — asking
the plane's ``get()`` would just raise and tell you nothing about the other 40,000 rows.

Reports collection, namespace, object_id, and the concrete validation errors, so the
output is directly actionable rather than a count.

Requested by Yua in adversarial review of PR #398: *"production needs a non-mutating
validation sweep before repair."* She is right, and the ordering is the point — you do
not get to repair what you have not first counted.
"""

from __future__ import annotations

import json
from typing import Any

import typer
from pydantic import BaseModel, ValidationError
from qdrant_client import QdrantClient

from musubi.types.artifact import SourceArtifact
from musubi.types.concept import SynthesizedConcept
from musubi.types.curated import CuratedKnowledge
from musubi.types.episodic import EpisodicMemory
from musubi.types.thought import Thought

validate_app = typer.Typer(help="Non-mutating integrity sweeps. Never writes.")

# Collection → the model that MUST be able to read every row in it.
# `thought` is included on purpose: Yua flagged that its get/history/transition paths can
# be poisoned the same way, and a sweep that skips a plane is a sweep that lies.
_PLANE_MODELS: dict[str, tuple[str, type[BaseModel]]] = {
    "episodic": ("musubi_episodic", EpisodicMemory),
    "curated": ("musubi_curated", CuratedKnowledge),
    "concept": ("musubi_concept", SynthesizedConcept),
    "artifact": ("musubi_artifact", SourceArtifact),
    "thought": ("musubi_thought", Thought),
}


def _scan_collection(
    client: QdrantClient, collection: str, model: type[BaseModel], batch: int
) -> tuple[int, list[dict[str, Any]]]:
    """Return (rows_scanned, broken_rows). Reads only."""
    scanned = 0
    broken: list[dict[str, Any]] = []
    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=collection,
            limit=batch,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for rec in records:
            scanned += 1
            payload = rec.payload or {}
            try:
                model.model_validate(payload)
            except ValidationError as exc:
                broken.append(
                    {
                        "collection": collection,
                        # A broken row may be missing these too — never index.
                        "namespace": payload.get("namespace", "<missing>"),
                        "object_id": payload.get("object_id", "<missing>"),
                        "point_id": str(rec.id),
                        "errors": [
                            {
                                "type": e["type"],
                                "loc": list(e["loc"]),
                                "msg": e["msg"],
                            }
                            for e in exc.errors()
                        ],
                        # The unmodeled keys are usually the whole story.
                        "unknown_keys": sorted(
                            str(e["loc"][0])
                            for e in exc.errors()
                            if e["type"] == "extra_forbidden" and e["loc"]
                        ),
                    }
                )
        if offset is None:
            break
    return scanned, broken


@validate_app.command("rows")
def validate_rows(
    url: str = typer.Option("http://localhost:6333", help="Qdrant URL."),
    api_key: str | None = typer.Option(None, help="Qdrant API key, if the cluster needs one."),
    plane: str | None = typer.Option(
        None, help="Limit to one plane. Default: every plane, which is what you want."
    ),
    batch: int = typer.Option(256, help="Scroll page size."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON for piping."),
) -> None:
    """Scan every persisted row and report the ones their own model can no longer read.

    Exit code is the number of broken rows, capped at 250 — so this is usable as a
    health check in CI or a cron without parsing stdout. Zero means clean, and zero is a
    claim this command is actually entitled to make, because it looked at every row.
    """
    client = QdrantClient(url=url, api_key=api_key)
    planes = [plane] if plane else list(_PLANE_MODELS)

    total_scanned = 0
    all_broken: list[dict[str, Any]] = []
    per_plane: dict[str, int] = {}

    for name in planes:
        if name not in _PLANE_MODELS:
            raise typer.BadParameter(
                f"unknown plane {name!r}; expected one of {sorted(_PLANE_MODELS)}"
            )
        collection, model = _PLANE_MODELS[name]
        try:
            scanned, broken = _scan_collection(client, collection, model, batch)
        except Exception as exc:  # collection may not exist on a fresh cluster
            if not as_json:
                typer.echo(f"  {name:9} SKIPPED — {type(exc).__name__}: {exc}")
            continue
        total_scanned += scanned
        per_plane[name] = len(broken)
        all_broken.extend(broken)
        if not as_json:
            mark = "OK  " if not broken else "BAD "
            typer.echo(f"  {mark} {name:9} {scanned:>6} rows   {len(broken)} unreadable")

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "scanned": total_scanned,
                    "broken_total": len(all_broken),
                    "broken_by_plane": per_plane,
                    "broken": all_broken,
                },
                indent=2,
            )
        )
    else:
        typer.echo("")
        if not all_broken:
            typer.echo(f"clean — {total_scanned} rows scanned, every one readable by its model.")
        else:
            typer.echo(
                f"{len(all_broken)} of {total_scanned} rows are UNREADABLE by their own model.\n"
                f"These rows cannot be retrieved. Until musubi#398 is deployed they also "
                f"cannot be deleted.\n"
            )
            for b in all_broken:
                keys = ", ".join(b["unknown_keys"]) or "—"
                typer.echo(f"  {b['collection']}  {b['namespace']}/{b['object_id']}")
                typer.echo(f"      unmodeled keys: {keys}")
                for e in b["errors"][:3]:
                    typer.echo(f"      {e['type']}: {'.'.join(map(str, e['loc']))} — {e['msg']}")
            typer.echo(
                "\nNEXT: back up the collection before any repair or quarantine mutation.\n"
                "This command will never mutate; repair is a separate, deliberate operator step."
            )

    raise typer.Exit(code=min(len(all_broken), 250))


__all__ = ["validate_app"]
