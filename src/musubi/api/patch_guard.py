"""The one rule every PATCH endpoint must obey: **never persist what you cannot read back.**

## The bug this exists to make structurally impossible

The PATCH endpoints write the request body straight into the Qdrant payload with
``set_payload``, and the payload is later read back through a strict (``extra="forbid"``)
model. Any gap between what the *request* model accepts and what the *persisted* model
accepts is a way to permanently destroy a row:

``set_payload`` **succeeds**, then the refreshing ``get()`` raises. The caller sees a 500
and concludes the write failed — while the row has already been bricked, is now unreadable
on every subsequent GET, and (before PR #398) could not even be deleted, because every
delete path guarded existence with a deserializing ``get()``.

Two distinct gaps have now been found, and the second one is why this module exists:

1. **Unknown keys.** The endpoints were ``extra="allow"`` behind a *denylist* of four or
   five field names. Every key nobody had imagined reached the payload. Fixed with an
   allowlist derived from the request model.

2. **Invalid values of KNOWN keys.** ``PatchEpisodicRequest.content`` was declared
   ``str | None`` while the persisted ``MemoryObject.content`` is ``Field(min_length=1)``.
   So ``{"content": ""}`` passed the allowlist, passed the request model, persisted — and
   then failed the read with ``string_too_short``. **The fix for (1) had introduced a
   fresh way to do exactly what (1) did.** (Yua, rev2 review of PR #398.)

## Why this is a guard and not a set of matching constraints

The obvious repair for (2) is to mirror ``min_length=1`` onto the request field. That
fixes the one gap that was found and leaves every *other* field's parity gap open — it is
a denylist of remembered mistakes, which is precisely the unsound pattern that caused (1).
It also silently rots the moment someone adds a constraint to a persisted model and
forgets this file exists.

So the guard does not enumerate constraints. It **simulates the write and reads it back**
before touching disk: merge the patch onto the current raw payload, validate the result
against the real persisted model, and only write if the row that would result is a row the
system can actually read.

    Never persist what you cannot read back.

That makes the invariant total rather than remembered. Any present or future divergence
between a request model and its persisted model becomes a clean 400 with a real message,
instead of a 500 and a dead memory.

**What this does NOT do: repair.** It prevents a readable row from being broken. It cannot
un-break an already-broken one — merging an allowed patch onto a payload that still holds
an unknown key correctly fails validation, and PATCH has no way to *remove* that key.
Repair of existing corrupt rows is a separate raw operator path (or a hard delete). Said
plainly so the next reader does not mistake this for a repair tool.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from musubi.api.errors import APIError


def reject_unknown_fields(incoming: dict[str, Any], patchable: set[str], *, plane: str) -> None:
    """Allowlist gate: a key the request model does not declare never reaches the payload."""
    unknown = set(incoming) - patchable
    if unknown:
        raise APIError(
            status_code=400,
            code="BAD_REQUEST",
            detail=(
                f"PATCH does not accept unknown fields: {sorted(unknown)}; patchable fields "
                f"are {sorted(patchable)}. An unmodeled key would be written to the {plane} "
                f"payload and then rejected by the read model, making the row permanently "
                f"unreadable."
            ),
        )


def assert_readable_after_patch(
    current_payload: dict[str, Any] | None,
    incoming: dict[str, Any],
    model: type[BaseModel],
    *,
    object_id: str,
) -> None:
    """Validate the row that WOULD result, before any of it is persisted.

    ``current_payload`` is the raw, un-deserialized payload (see
    :mod:`musubi.store.raw_lookup`) — raw on purpose, so reading it does not itself blow
    up on an already-corrupted row.

    **Scope, stated precisely: this PREVENTS FURTHER CORRUPTION. It does not repair.**

    An earlier draft of this docstring claimed the guard "still works while repairing an
    already-corrupted row." That overclaimed. If the stored payload already carries an
    unknown key, merging an allowed patch onto it still fails canonical validation — and
    it *should*, because the merged row genuinely would not be readable. PATCH cannot
    remove an unknown key; nothing in the write path can. Repairing an existing corrupt
    row needs a separate raw operator path (or a hard delete). The honest statement is:
    a clean row can never be broken through here, and a broken row cannot be healed
    through here either. (Yua, rev3 review of PR #398 — a doc that promises more than the
    code delivers is the same class of defect as the code that started this.)

    Raises ``APIError(400)`` if the merged row would not satisfy ``model``. Writes nothing
    either way; the caller only reaches ``set_payload`` if this returns.
    """
    if current_payload is None:
        # Existence is the caller's 404 to raise; nothing to validate against.
        return

    merged = {**current_payload, **incoming}
    try:
        model.model_validate(merged)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise APIError(
            status_code=400,
            code="BAD_REQUEST",
            detail=(
                f"PATCH refused: the resulting row would not be readable by "
                f"{model.__name__} — {problems}. Nothing was written; {object_id!r} is "
                f"unchanged. (A write that cannot be read back is how a memory gets "
                f"permanently destroyed: set_payload succeeds, the refresh read raises, "
                f"and the caller believes the write failed.)"
            ),
        ) from exc


__all__ = ["assert_readable_after_patch", "reject_unknown_fields"]
