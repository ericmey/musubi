---
title: "ADR 0042: Escrow-backed episodic retraction"
section: 13-decisions
type: adr
status: proposed
date: 2026-08-03
updated: 2026-08-03
deciders: [Eric]
tags: [architecture, api, artifacts, idempotency, data-001, type/adr, status/proposed]
supersedes: []
superseded-by:
---

# ADR 0042: Escrow-backed episodic retraction

**Status:** proposed
**Date:** 2026-08-03
**Decider:** Eric

## Context

Episodic create rejects `content` above 32,768 UTF-8 bytes. The historical
retraction client instead reads a memory, places the complete original inside a
replacement tombstone, and sends ordinary PATCH. That preserves evidence and
keeps the original retrieval vector, but it makes PATCH unbounded. Applying the
create limit without a replacement path would make an existing oversized false
memory impossible to neutralize.

The original cannot be recovered from immutable-vector history. A successful
vector-changing publish deletes every superseded content point except the live
one. Backups may retain bytes, but no supported live API can address them.

Eric selected **Option A: artifact escrow**. Before retraction, Musubi must save
the exact original content bytes as an artifact that is readable by explicit
reference and deliberately absent from semantic search. A bounded tombstone then
replaces the logical episodic content without re-embedding. Option B, which would
discard the addressable original and retain only a cryptographic commitment, is
not authorized, including as an error-path fallback.

The storage layout makes the vector rule precise. On v2, the immutable content
point already contains the original projection text and the vector derived from
that text. Retraction changes only the authoritative anchor. The content point
remains a faithful, self-describing vector snapshot; the deliberate divergence
and its evidence live on the anchor. On legacy v1, the single physical row keeps
its existing vector while its payload becomes the tombstone.

## Decision

### 1. Add a truthful stored-unindexed artifact state

Add `stored_unindexed` to `ArtifactIndexingState`.

A `stored_unindexed` `SourceArtifact`:

- has `chunk_count == 0`;
- has no `committed_generation`, `committed_owner`, or
  `index_operation_id`;
- has no `failure_reason`;
- is readable by artifact id and blob endpoint; and
- never admits an indexing intent.

Artifact search remains chunk-based and committed-head filtered. A head with no
committed generation exposes zero chunks. Artifact metadata keeps its existing
zero dense vector. These structural facts, not an application-side exclusion
list, make escrow absent from semantic search.

The metadata `title` and `filename` are deterministic synthetic identifiers
derived from the source episodic object and escrow generation. They never contain
the original content, summary, or caller prose. Ordinary artifact upload and its
`indexing -> indexed|failed` behavior do not change.

### 2. Make the blob the first durable boundary

The escrow bytes are exactly:

```text
canonical_current_episodic.content.encode("utf-8")
```

Musubi computes the SHA-256 and byte length server-side. A caller cannot supply
or override either value.

The escrow artifact id is deterministic for a domain-separated tuple containing
the authorized episodic namespace, source object id, and exact original digest.
It is a valid KSUID and therefore fits the existing artifact model and routes:
retain the source KSUID's four timestamp bytes and use the first 16 bytes of
`SHA-256(b"musubi-retraction-escrow-v1\x00" || namespace_utf8 || 0x00 ||
object_id_ascii || 0x00 || original_digest_bytes)` as the KSUID payload, where
`original_digest_bytes` is the 32-byte value decoded from lowercase hex. Golden
tests freeze this encoding; changing it requires a new ADR revision because it
is the crash-recovery address.

Because the four timestamp bytes come from the source episodic KSUID, escrow
artifact ids sort by source-object creation time, not escrow creation time.
Recency and retention logic must use explicit artifact metadata/state rather than
interpreting KSUID time as the escrow's age.

Publication order is binding:

1. derive the sibling artifact namespace and deterministic artifact id;
2. if the final blob exists, verify it and either reuse it or fail closed;
3. otherwise write a unique temporary blob in the final blob directory;
4. flush and fsync the file, publish it atomically with no-clobber semantics,
   then fsync the directory; a concurrent winner is handled as an existing blob,
   never overwritten;
5. read the final blob back and verify exact length and SHA-256;
6. publish the `stored_unindexed` artifact head;
7. read the head back and verify id, namespace, state, digest, length, and
   synthetic metadata; then
8. permit episodic retraction to begin.

No externally readable artifact head exists before exact blob readback succeeds.
A failure after blob publication but before head publication may leave an
unreachable blob. The deterministic retry reuses it only after exact digest and
length verification. Mere path or head existence is never success: a truncated,
torn, or divergent blob at the deterministic address fails closed.

That failure is recoverable only through explicit operator hard purge of the
exact derived artifact namespace and id. The failure response identifies that
address without exposing original content. Before purge, the operator must prove
that no committed episodic `retraction_evidence` references it; a referenced
escrow is not eligible for this recovery. The existing idempotent artifact purge
removes the exact blob, head, and chunks even when publication stopped before a
head existed. After purge, a retry must observe the address absent and recreate
it through the same no-clobber publication protocol. The saga never invokes this
recovery automatically.

The verified blob plus exact artifact head are the durable journal for the
escrow-before-tombstone midpoint. No second SQLite saga row is added unless an
implementation spike proves this deterministic recovery contract insufficient.

Automatic artifact retention never archives or purges `stored_unindexed`
retraction escrow. A referenced escrow is part of the supported retraction
record. An unreferenced escrow left by a stale-version loser may be collected
only by a separate reverse-reachability policy that proves no episodic evidence
names it. Operator hard purge remains an explicit destructive authority, but it
must not be mistaken for ordinary retention or silently triggered by this saga.

### 3. Authorize both planes before observing the original

The endpoint accepts one concrete episodic namespace. It derives the sibling
artifact namespace by preserving the first two namespace segments and replacing
only the plane segment with `artifact`.

The same authenticated request must have write authority for both namespaces.
Both authorization checks complete before Musubi:

- reads the episodic row;
- hashes or measures its content;
- checks for an existing escrow blob or head; or
- touches either storage plane.

The caller cannot provide a different artifact namespace. Authentication and
both authorization failures reach no raw read, filesystem call, Qdrant call, or
idempotency acquisition that could reveal target state.

### 4. Store strict retraction evidence on the logical row

Add a strict, optional `retraction_evidence` domain field. Its v1 shape contains:

- `kind = "artifact_escrow_v1"`;
- derived artifact namespace and `ArtifactRef`;
- exact original SHA-256 and UTF-8 byte length;
- exact quoted-prefix byte length and `omitted_bytes`;
- `vector_basis = "original"`;
- the preserved `live_point` for v2, or an explicit legacy self-pointer marker;
- an opaque hash of the complete authorized idempotency identity; and
- the canonical request digest.

The raw idempotency key is never persisted or returned. The operation-identity
hash and request digest are terminal commit markers: after a crash that lands the
tombstone but loses the response or durable receipt, the exact retry can identify
and adopt its already-committed result. A different identity or digest cannot.

The tombstone content is server-built from a fixed retraction marker, bounded
caller-owned truth fields, a grapheme-safe original prefix, and the artifact
reference/digest evidence. The complete UTF-8 encoding is at most 32,768 bytes.
`omitted_bytes` equals original byte length minus the exact prefix bytes carried in
the tombstone. Summary and tags are bounded and server-normalized. Callers never
supply a raw tombstone, artifact reference, digest, byte count, vector basis, or
operation marker.

On v2, the commit changes only the anchor payload/version. `live_point`,
`committed_operation_id`, the content point, its generation, and all vectors stay
unchanged. No metadata is added to the write-once content point. On legacy v1,
the same one-shot non-reembedding seam changes the single payload/version while
leaving vectors intact.

VAL-002 continues to reject ordinary projection divergence. It permits divergence
only when strict evidence is present and locally proves that:

- the preserved pointer is still current;
- the anchor operation still equals content generation;
- the target content hash and byte length equal the recorded original; and
- quoted-prefix and omitted-byte accounting reconcile.

Malformed, partial, stale, or tag-only evidence cannot waive the invariant.

### 5. Expose one escrow-first retraction saga

Add a dedicated endpoint:

```text
POST /v1/episodic/{object_id}/retract?namespace=<episodic-namespace>
```

The request requires `Idempotency-Key` and `expected_version`. It carries only
caller-owned truth inputs: retraction date, reason, replacement truth, optional
summary/tags, and an optional superseding object reference.

The endpoint always uses durable completed-response receipt semantics. It adds
its route operation to the durable-receipt eligibility registry; durable mode is
not optional for this new destructive-of-active-payload operation. The final 2xx
body includes the episodic object id, new version, escrow artifact reference, and
typed evidence, so it satisfies the completed-response receipt contract.

Execution order is:

1. authenticate and authorize both namespaces;
2. acquire the idempotency lease for the exact request;
3. read and validate the canonical episodic object and expected version;
4. create or exact-readback-reuse the deterministic escrow;
5. build and pre-validate the bounded tombstone/evidence;
6. commit one namespace-bound, layout-aware, one-shot non-reembedding mutation;
7. exact-readback the operation identity, request digest, version, and evidence;
8. return the completed response, which the observer receipts before releasing
   success.

The handler checks for an already-committed matching operation before treating
the row as generically already retracted or stale. This closes the crash after
tombstone commit but before response/receipt. A matching identity and digest
returns the same logical completed response. Any different retraction identity
or digest against a row that already carries `retraction_evidence` returns a
typed conflict; it cannot overwrite the first evidence, orphan its escrow, or
form a retraction chain.

Escrow failure causes zero episodic mutation. A stale expected version after
escrow returns typed 409 and leaves a safe stored-unindexed artifact for exact
retry or later retention cleanup. The endpoint never silently rebases, never
deletes the episodic object, and never falls back to evidence discard.

Once this endpoint is deployed, ordinary episodic PATCH enforces the existing
32,768-byte typed limit on incoming `content` replacement. A metadata-only PATCH
against an already-oversized legacy row remains allowed because it does not create
new oversized content. V2 `content` and `summary` PATCH remain typed-refused and
direct callers to the dedicated endpoint.

### 6. Cut every active consumer over before closure

The canonical fleet `memory-data musubi retract` command moves to the dedicated
endpoint. It sends the observed expected version and a stable idempotency key and
stops constructing the full-original tombstone client-side.

The current census found no second active raw retraction producer in
musubi-codex, musubi-claude, or musubi-harness. The consumer slice keeps that
closure checked and proves the installed marketplace executable is byte-identical
to the reviewed source. Source-checkout tests alone are insufficient.

Existing inline full-original tombstones remain valid legacy episodic rows. This
decision does not migrate them, manufacture retroactive artifacts, or reinterpret
them as malformed evidence.

## Binding conditions

1. **A1 — no indexing:** escrow has zero chunks and no indexing intent; search
   absence is proven with an indexed positive control.
2. **A2 — bytes before head:** no artifact head is visible until final blob
   digest and length readback succeeds.
3. **A3 — existence is not verification:** deterministic retry accepts only
   exact blob and head readback.
4. **A4 — both auth checks first:** neither plane is observed before both write
   authorities are established.
5. **A5 — anchor-only exception:** v2 content point text, vector, generation, and
   payload remain immutable.
6. **A6 — strict evidence:** only the typed, fully bound evidence shape permits
   anchor/content divergence.
7. **A7 — two crash markers:** deterministic artifact identity recovers the
   escrow-only midpoint; operation identity plus request digest recover a landed
   tombstone whose response/receipt was lost.
8. **A8 — one-shot commit:** stale expected version returns 409 and never rebases.
9. **A9 — no discard fallback:** any escrow uncertainty blocks episodic mutation.
10. **A10 — deployed consumer proof:** umbrella closure requires installed-path
    parity and cold invocation, not only a fleet-tools source diff.
11. **A11 — referenced escrow retention:** automatic cleanup cannot delete a
    stored-unindexed artifact named by retraction evidence; orphan cleanup needs
    reverse-reachability proof.
12. **A12 — corrupt-address recovery is explicit:** a deterministic-address
    mismatch blocks retraction until an operator proves it unreferenced, hard
    purges that exact escrow address, and a retry observes it absent.
13. **A13 — re-retraction cannot replace evidence:** only an exact identity and
    digest replay may adopt an existing retraction; every distinct attempt is a
    typed conflict.

## Consequences

### Positive

- Oversized false memories remain retractable without unbounded episodic content.
- The complete original stays addressable under the artifact retention and backup
  contract but is not made semantically searchable on a second plane.
- Retrieval by the remembered false claim still finds the neutralized episodic
  row because the original vector remains the retrieval basis.
- Both cross-storage crash midpoints have deterministic, inspectable recovery.
- VAL-002 distinguishes a deliberate retraction from accidental projection drift
  without weakening strict validation for ordinary rows.

### Negative

- Retraction becomes a cross-plane saga with filesystem, Qdrant, authorization,
  validation, and idempotency failure modes.
- Stored-unindexed blobs may remain after a stale-version loser or head-publication
  crash. They are safe and unreachable/search-invisible, but retention cleanup
  needs an explicit policy rather than best-effort deletion inside the commit path.
- Tokens limited to episodic write but not sibling artifact write can no longer
  retract until their scope is widened deliberately.

### Neutral

- Ordinary artifact upload/indexing is unchanged.
- Existing inline tombstones remain readable and valid with no migration.
- The household remains single-worker until #558 supplies distributed idempotency
  ownership; this ADR does not claim a completed receipt solves that broader gap.

## Alternatives considered

### Explicit policy discard

Rejected by the owner. A hash and byte length are a cryptographic commitment, not
an addressable original. Discard is not permitted as a shortcut or outage mode.

### Publish through the immutable-vector writer

Rejected because a normal vector-changing publish re-embeds the tombstone. That
would make retraction harder to retrieve by the false claim it neutralized and
would delete the original live content snapshot.

### Mint tombstone text on a new content point while retaining the old vector

Rejected because a content point is a faithful snapshot of the text that produced
its vector. This alternative would silently break that invariant. The accepted
anchor-only exception keeps the content point truthful and makes divergence typed.

### Reuse ordinary artifact upload

Rejected because it publishes the head before blob durability and always enqueues
indexing, which can make the false original searchable on the artifact plane.

### Treat DurableReceiptStore as the saga journal

Rejected because it stores only completed successful responses. It cannot describe
the escrow-before-tombstone midpoint. It remains the terminal replay proof.

### Add a separate SQLite saga row immediately

Deferred. Deterministic artifact identity plus exact blob/head readback already
provides durable midpoint state. Add another journal only if a spike proves a
specific recovery property this contract cannot meet.

## Implementation graph

- #643 — stored-unindexed artifact state.
- #644 — exact-byte deterministic escrow primitive.
- #645 — typed retraction evidence and VAL-002 exception.
- #646 — dedicated endpoint saga and ordinary PATCH size parity.
- `ericmey/fleet-tools#32` — canonical client cutover and installed-path proof.
- #611 — umbrella; remains open until every lane closes.

## References

- [[13-decisions/0009-artifact-metadata-in-qdrant]]
- [[13-decisions/0036-artifact-committed-generation-indexing]]
- [[13-decisions/0039-durable-client-idempotency-receipts]]
- [[13-decisions/data001-phase2-immutable-vectors]]
- [[_slices/slice-api-v1-data001-episodic-patch-fence]]
- Issue #611 decision packet at commit `eaf3b9a`, SHA-256
  `478e4de3db2a2195a9bb244ef33c6157ffb96c40ae80013eaa99b4e6a0730477`.
