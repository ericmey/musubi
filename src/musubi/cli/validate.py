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

**A missing canonical collection counts as NOT SCANNED.** The second thing this file got
wrong. ``absent`` was treated as benign — "an empty plane, nothing to see" — so pointing
the command at the wrong Qdrant node found no collections at all and printed
``clean — 0 rows scanned across 7 plane(s)``, exit 0. The test guarding that behaviour was
named ``test_absent_collection_is_not_an_error_and_not_a_lie`` while *asserting the lie*.
(Yua, rev3 review.)

Every collection in ``store/names.py`` is canonical and ``store/collections.py`` bootstraps
all of them. A missing one is not an empty plane — it is an unbootstrapped, damaged, or
**wrong** node. Not evidence that production is clean; evidence that we are not looking at
production.

## Coverage and integrity are SEPARATE AXES

The third thing this file got wrong, and the root of the other two: it folded "did I see
everything?" and "was what I saw sound?" into a single verdict. So ``--allow-absent``
mutated the completeness flag, which drove the verdict, which drove the summary — and a run
that skipped a collection AND found a broken row printed ``clean-partial … every one
readable by its model`` immediately above ``1 of 1 scanned rows are UNREADABLE``. The output
contradicted itself in adjacent sentences. (Yua, rev4 review.)

    coverage:  full | partial | incomplete     — did I see everything?
    integrity: clean | broken | unknown        — was what I saw sound?

They are reported independently and combined into one unambiguous verdict:

    clean | broken | clean-partial | broken-partial | incomplete

Coverage is **never** ``full`` while a canonical collection is missing, whatever flag was
passed — ``--allow-absent`` changes whether we PROCEED, not whether we LOOKED. Integrity is
``unknown`` whenever coverage is incomplete: if we did not look, we get no opinion. And the
human output never claims every row is readable while unreadable rows exist.

Only exit 0 is a production integrity pass.
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

# Exit codes. A caller must be able to distinguish, from the number alone:
#   "I looked at everything and it is fine"
#   "I looked at everything and found N bad rows"
#   "I could not look"
#   "I looked at SOME of it" — with or without findings
# None of those may ever collapse into the same number.
#
#   0        clean          — full coverage, nothing unreadable
#   1..250   N broken rows  — FULL coverage, that many unreadable
#   251      incomplete     — coverage failed; integrity UNKNOWN
#   252      clean-partial  — accepted absence; nothing bad in what WAS scanned
#   253      broken-partial — accepted absence AND unreadable rows found (count in output)
#
# The partial codes sit above the broken cap on purpose. `broken-partial` must NOT collapse
# into an ordinary fully-scanned broken count: "I found 2 bad rows and saw everything" and
# "I found 2 bad rows but skipped a collection" are different facts, and a CI gate reading
# only the exit code must not confuse them. (Yua, rev4 review of PR #398.)
EXIT_CLEAN = 0
_MAX_BROKEN_EXIT = 250
EXIT_INCOMPLETE = 251
EXIT_CLEAN_PARTIAL = 252
EXIT_BROKEN_PARTIAL = 253


@dataclass
class PlaneResult:
    """What actually happened to one plane. `status` is the load-bearing field."""

    plane: str
    collection: str
    status: str  # "scanned" | "absent" | "error"
    scanned: int = 0
    broken: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

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
    allow_absent: bool = typer.Option(
        False,
        "--allow-absent",
        help=(
            "Permissive diagnostic mode: tolerate missing canonical collections (a fresh "
            "or partially bootstrapped cluster). The verdict becomes `clean-partial`, "
            "never `clean` — this is NOT a production integrity pass."
        ),
    ),
) -> None:
    """Scan every persisted row and report the ones their own model can no longer read.

    Coverage and integrity are reported as SEPARATE axes, because they are separate
    questions and folding them together is how this command last lied:

      coverage:  full | partial | incomplete     — did I see everything?
      integrity: clean | broken | unknown        — was what I saw sound?

    Exit codes:
      0        clean          — full coverage, nothing unreadable.
      1..250   N broken rows  — FULL coverage, that many unreadable.
      251      incomplete     — could not scan something. Integrity is UNKNOWN.
      252      clean-partial  — --allow-absent; nothing bad in what WAS scanned.
      253      broken-partial — --allow-absent AND unreadable rows found.

    Only exit 0 is a production integrity pass.
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
    absent = [r for r in results if r.status == "absent"]
    errored = [r for r in results if r.status == "error"]
    total_scanned = sum(r.scanned for r in results)

    # TWO INDEPENDENT AXES. Collapsing them is what produced the last bug.
    #
    #   COVERAGE  — did I see everything?     full | partial | incomplete
    #   INTEGRITY — was what I saw sound?     clean | broken  | unknown
    #
    # The previous cut folded `--allow-absent` into a single `incomplete` list, which then
    # defined `complete`, which then drove the verdict AND the human summary. So an
    # accepted absence made `complete: true` (it was not), and any absence printed
    # "every one readable" — directly above the list of unreadable rows. The output
    # contradicted itself in adjacent sentences. (Yua, rev4 review of PR #398.)
    #
    # Coverage is NEVER "full" while a canonical collection is missing, no matter what
    # flag was passed. `--allow-absent` changes whether we PROCEED, not whether we LOOKED.
    if errored or (absent and not allow_absent):
        coverage = "incomplete"
    elif absent:
        coverage = "partial"  # absence explicitly accepted — but still not full coverage
    else:
        coverage = "full"

    if coverage == "incomplete":
        integrity = "unknown"  # we did not look; we get no opinion
    elif all_broken:
        integrity = "broken"
    else:
        integrity = "clean"

    # A single unambiguous combined verdict, so no consumer has to reason about the pair.
    verdict = {
        ("full", "clean"): "clean",
        ("full", "broken"): "broken",
        ("partial", "clean"): "clean-partial",
        ("partial", "broken"): "broken-partial",
        ("incomplete", "unknown"): "incomplete",
    }[(coverage, integrity)]

    if as_json:
        typer.echo(
            json.dumps(
                {
                    # Verdict FIRST and explicit. A consumer must not have to infer health
                    # from an empty `broken` list, nor coverage from a verdict string.
                    "verdict": verdict,
                    "coverage": coverage,
                    "integrity": integrity,
                    # `complete` means FULL coverage. Never true with a canonical
                    # collection missing, accepted or not.
                    "complete": coverage == "full",
                    "absent_collections": [r.collection for r in absent],
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
                typer.echo(f"  FAIL {r.plane:15} {'':>6}       SCAN FAILED — {r.error}")
            elif r.status == "absent":
                typer.echo(f"  GONE {r.plane:15} {'':>6}       CANONICAL COLLECTION MISSING")
            else:
                mark = "OK  " if not r.broken else "BAD "
                typer.echo(f"  {mark} {r.plane:15} {r.scanned:>6} rows  {len(r.broken)} unreadable")
        typer.echo("")

        # COVERAGE first, INTEGRITY second — always both, always in that order, and never
        # a sentence that claims one while the other contradicts it.
        typer.echo(f"VERDICT: {verdict}   (coverage: {coverage}, integrity: {integrity})")
        typer.echo("")

        # --- coverage ---
        if coverage == "incomplete":
            errs = ", ".join(r.plane for r in errored)
            gone = ", ".join(r.plane for r in absent)
            typer.echo("COVERAGE — INCOMPLETE. This run makes NO claim about integrity.")
            if errs:
                typer.echo(f"  could not scan: {errs}")
            if gone:
                typer.echo(
                    f"  canonical collections MISSING: {gone}\n"
                    f"  Every collection in store/names.py is canonical and is bootstrapped by\n"
                    f"  store/collections.py. A missing one does not mean an empty plane — it\n"
                    f"  means this is an unbootstrapped, damaged, or WRONG Qdrant node. You are\n"
                    f"  not looking at production."
                )
            typer.echo(
                "  Fix it and re-run. Do not treat this as clean.\n"
                "  (--allow-absent only for a knowingly fresh/partial cluster; even then the\n"
                "   verdict is clean-partial / broken-partial, never a production pass.)"
            )
        elif coverage == "partial":
            gone = ", ".join(r.plane for r in absent)
            typer.echo(
                f"COVERAGE — PARTIAL. These canonical collections are MISSING and were never\n"
                f"  scanned: {gone}. (--allow-absent was set.)\n"
                f"  This is NOT a production integrity pass — whatever is in those collections\n"
                f"  is unknown."
            )
        else:
            typer.echo(
                f"COVERAGE — FULL. All {len(results)} canonical collection(s) scanned end to end."
            )

        # --- integrity, stated separately and only about what was ACTUALLY scanned ---
        typer.echo("")
        if integrity == "unknown":
            typer.echo(
                f"INTEGRITY — UNKNOWN. {len(all_broken)} unreadable row(s) turned up in the "
                f"planes that did scan,\n  but coverage is incomplete, so the real number is "
                f"not known."
            )
        elif integrity == "clean":
            typer.echo(
                f"INTEGRITY — CLEAN. All {total_scanned} rows THAT WERE SCANNED are readable "
                f"by their model."
            )
        else:
            typer.echo(
                f"INTEGRITY — BROKEN. {len(all_broken)} of {total_scanned} scanned rows are "
                f"UNREADABLE by their own\n  model. These rows cannot be retrieved."
            )

        if all_broken:
            typer.echo("")
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

    # Coverage outranks integrity: if we did not look, we do not get to report a count.
    if coverage == "incomplete":
        raise typer.Exit(code=EXIT_INCOMPLETE)
    if coverage == "partial":
        raise typer.Exit(code=EXIT_BROKEN_PARTIAL if all_broken else EXIT_CLEAN_PARTIAL)
    raise typer.Exit(code=min(len(all_broken), _MAX_BROKEN_EXIT))


__all__ = ["EXIT_CLEAN", "EXIT_INCOMPLETE", "validate_app"]
