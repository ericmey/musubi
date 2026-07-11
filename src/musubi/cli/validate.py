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

## "Clean" is a claim, and this command must earn it

The first cut of this file caught every exception from a collection scan, continued, and
then printed ``clean`` and exited 0 whenever no broken rows had been *collected* — which
included the case where **auth failed, the network failed, or every collection was
misnamed and nothing was scanned at all.** A sweep that reports the vault healthy because
it could not look at the vault is the exact defect this whole PR exists to fix, rebuilt
one layer up. (Caught by Yua, rev2 review. It was the third instance of this shape in a
single day: the vault's stale-check reported "0 stale" while reading 8 of 164 pages, the
frontmatter lint could never pass so was never run, and then this.)

So the invariant here is structural, not a promise:

    A plane that was not fully scanned CANNOT be counted as clean.
    Any incomplete scan makes the whole run INCOMPLETE and the exit code non-zero.
    ``clean`` is printed only when every requested plane was scanned end to end.

An absent *optional* collection (a plane never bootstrapped on this cluster) is a
distinct, benign outcome — reported as ``absent``, not silently folded into success and
not treated as a failure. Everything else that stops a scan is an **error**, and an error
anywhere means this command does not get to say the word "clean".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import typer
from pydantic import BaseModel, ValidationError
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse

from musubi.types.artifact import ArtifactChunk, SourceArtifact
from musubi.types.concept import SynthesizedConcept
from musubi.types.curated import CuratedKnowledge
from musubi.types.episodic import EpisodicMemory
from musubi.types.lifecycle_event import LifecycleEvent
from musubi.types.thought import Thought

validate_app = typer.Typer(help="Non-mutating integrity sweeps. Never writes.")

# Collection → the model that MUST be able to read every row in it.
#
# ALL SEVEN canonical collections in `store/names.py`. The first cut covered five and
# quietly omitted `musubi_artifact_chunks` and `musubi_lifecycle_events` — both of which
# have strict payload models and active read paths. A sweep that skips a collection is a
# sweep that lies, and it lies in the most dangerous direction: it reports clean.
#
# If you add a collection to `store/names.py`, add it here. `test_every_canonical_
# collection_is_swept` fails if you don't — the mapping cannot silently fall behind.
_PLANE_MODELS: dict[str, tuple[str, type[BaseModel]]] = {
    "episodic": ("musubi_episodic", EpisodicMemory),
    "curated": ("musubi_curated", CuratedKnowledge),
    "concept": ("musubi_concept", SynthesizedConcept),
    "artifact": ("musubi_artifact", SourceArtifact),
    "artifact_chunks": ("musubi_artifact_chunks", ArtifactChunk),
    "thought": ("musubi_thought", Thought),
    "lifecycle": ("musubi_lifecycle_events", LifecycleEvent),
}

# Exit codes. A caller must be able to tell "I looked and it is fine" from "I could not
# look", and those must never be the same number.
#
#   0        clean — every requested plane scanned end to end, nothing unreadable
#   1..250   that many unreadable rows (capped)
#   251      INCOMPLETE — a scan failed; the result is UNKNOWN
#
# EXIT_INCOMPLETE sits ABOVE the broken cap on purpose. The obvious choice (2) would be
# indistinguishable from "2 broken rows found" — an exit code that means two different
# things is an instrument that lies, which is the entire subject of this PR.
EXIT_CLEAN = 0
_MAX_BROKEN_EXIT = 250
EXIT_INCOMPLETE = 251


@dataclass
class PlaneResult:
    """What actually happened to one plane. `status` is the load-bearing field."""

    plane: str
    collection: str
    status: str  # "scanned" | "absent" | "error"
    scanned: int = 0
    broken: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def complete(self) -> bool:
        """Did we actually see every row? `absent` counts: there was nothing to see."""
        return self.status in ("scanned", "absent")

    def as_dict(self) -> dict[str, Any]:
        return {
            "plane": self.plane,
            "collection": self.collection,
            "status": self.status,
            "scanned": self.scanned,
            "broken": len(self.broken),
            "error": self.error,
        }


def _collection_missing(exc: Exception) -> bool:
    """Is this "the collection isn't here" rather than "I could not reach Qdrant"?

    Only a 404 from Qdrant means absent. Anything else — auth, timeout, DNS, a 500 — is
    an operational failure and must NOT be mistaken for an empty plane.
    """
    if isinstance(exc, UnexpectedResponse):
        return exc.status_code == 404
    return False


def _scan_collection(
    client: QdrantClient, plane: str, collection: str, model: type[BaseModel], batch: int
) -> PlaneResult:
    """Scan one collection. Reads only. Never raises — records what happened instead."""
    result = PlaneResult(plane=plane, collection=collection, status="scanned")
    offset = None
    while True:
        try:
            records, offset = client.scroll(
                collection_name=collection,
                limit=batch,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            if _collection_missing(exc) and result.scanned == 0:
                result.status = "absent"
                return result
            # A failure PART-WAY through pagination is the nastiest case: we have real
            # rows and real findings, but we did NOT see everything. Keep what we found
            # and mark the plane incomplete — partial results must never read as total.
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            return result

        for rec in records:
            result.scanned += 1
            payload = rec.payload or {}
            try:
                model.model_validate(payload)
            except ValidationError as exc:
                result.broken.append(
                    {
                        "collection": collection,
                        # A broken row may be missing these too — never index.
                        "namespace": payload.get("namespace", "<missing>"),
                        "object_id": payload.get("object_id", "<missing>"),
                        "point_id": str(rec.id),
                        "errors": [
                            {"type": e["type"], "loc": list(e["loc"]), "msg": e["msg"]}
                            for e in exc.errors()
                        ],
                        "unknown_keys": sorted(
                            str(e["loc"][0])
                            for e in exc.errors()
                            if e["type"] == "extra_forbidden" and e["loc"]
                        ),
                    }
                )
        if offset is None:
            return result


@validate_app.command("rows")
def validate_rows(
    url: str = typer.Option("http://localhost:6333", help="Qdrant URL."),
    api_key: str | None = typer.Option(
        None,
        envvar="QDRANT_API_KEY",
        help=(
            "Qdrant API key, if the cluster needs one. PREFER the QDRANT_API_KEY env var: "
            "a key passed as a flag lands in argv, which is visible in `ps` and in shell "
            "history."
        ),
    ),
    plane: str | None = typer.Option(
        None, help="Limit to one plane. Default: every plane, which is what you want."
    ),
    batch: int = typer.Option(256, help="Scroll page size. Must be > 0."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON for piping."),
) -> None:
    """Scan every persisted row and report the ones their own model can no longer read.

    Exit codes:
      0        clean — every requested plane scanned end to end, no unreadable rows.
      1..250   that many unreadable rows found.
      251      INCOMPLETE — at least one plane could not be scanned. The result is
               UNKNOWN. This is not "clean" and must never be treated as such.
    """
    if batch <= 0:
        raise typer.BadParameter("batch must be > 0", param_hint="--batch")

    planes = [plane] if plane else list(_PLANE_MODELS)
    for name in planes:
        if name not in _PLANE_MODELS:
            raise typer.BadParameter(
                f"unknown plane {name!r}; expected one of {sorted(_PLANE_MODELS)}",
                param_hint="--plane",
            )

    # Construction itself can fail (bad URL). That is an incomplete run, not a clean one.
    try:
        client = QdrantClient(url=url, api_key=api_key)
    except Exception as exc:
        results = [
            PlaneResult(
                plane=n,
                collection=_PLANE_MODELS[n][0],
                status="error",
                error=f"{type(exc).__name__}: {exc}",
            )
            for n in planes
        ]
    else:
        results = [
            _scan_collection(client, n, _PLANE_MODELS[n][0], _PLANE_MODELS[n][1], batch)
            for n in planes
        ]

    all_broken = [b for r in results for b in r.broken]
    incomplete = [r for r in results if not r.complete]
    total_scanned = sum(r.scanned for r in results)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    # The verdict is FIRST and explicit. A consumer must not have to
                    # infer health from an empty `broken` list.
                    "complete": not incomplete,
                    "verdict": (
                        "incomplete" if incomplete else ("clean" if not all_broken else "broken")
                    ),
                    "scanned": total_scanned,
                    "broken_total": len(all_broken),
                    # Every requested plane appears, including the ones that failed.
                    "planes": [r.as_dict() for r in results],
                    "broken": all_broken,
                },
                indent=2,
            )
        )
    else:
        for r in results:
            if r.status == "error":
                typer.echo(f"  FAIL {r.plane:9} {'':>6}        SCAN FAILED — {r.error}")
            elif r.status == "absent":
                typer.echo(f"  --   {r.plane:9} {'':>6}        collection absent")
            else:
                mark = "OK  " if not r.broken else "BAD "
                typer.echo(f"  {mark} {r.plane:9} {r.scanned:>6} rows   {len(r.broken)} unreadable")
        typer.echo("")

        if incomplete:
            names = ", ".join(r.plane for r in incomplete)
            typer.echo(
                f"INCOMPLETE — could not scan: {names}.\n"
                f"This run makes NO claim about integrity. {len(all_broken)} unreadable "
                f"row(s) were found in the planes that did scan, but the planes above were "
                f"not read at all, so the real number is UNKNOWN.\n"
                f"Fix the connection/auth/collection problem and re-run. Do not treat this "
                f"as clean."
            )
        elif not all_broken:
            typer.echo(
                f"clean — {total_scanned} rows scanned across {len(results)} plane(s), "
                f"every one readable by its model."
            )

        if all_broken:
            typer.echo(
                f"\n{len(all_broken)} of {total_scanned} scanned rows are UNREADABLE by "
                f"their own model. These rows cannot be retrieved.\n"
            )
            for b in all_broken:
                keys = ", ".join(b["unknown_keys"]) or "—"
                typer.echo(f"  {b['collection']}  {b['namespace']}/{b['object_id']}")
                typer.echo(f"      unmodeled keys: {keys}")
                for e in b["errors"][:3]:
                    typer.echo(f"      {e['type']}: {'.'.join(map(str, e['loc']))} — {e['msg']}")
            typer.echo(
                "\nNEXT: back up the collection before any repair or quarantine mutation.\n"
                "This command will never mutate; repair is a separate, deliberate step."
            )

    # An incomplete scan outranks a clean count. We do not know, and the exit code says so.
    if incomplete:
        raise typer.Exit(code=EXIT_INCOMPLETE)
    raise typer.Exit(code=min(len(all_broken), _MAX_BROKEN_EXIT))


__all__ = ["EXIT_CLEAN", "EXIT_INCOMPLETE", "validate_app"]
