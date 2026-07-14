# ART-001 Qdrant global-search publication discriminator

## Pre-encoding API and design drift

Observed on a disposable digest-pinned Qdrant 1.17.1 server before the branch's
first edit:

- one independent Python reader issued 3,088 exact-K queries while another
  client submitted one `delete_alias` + `create_alias` update;
- every result was wholly from the old collection or wholly from the new
  collection; there were zero mixed results, empty gaps, or query errors;
- Qdrant's collection documentation explicitly says multiple alias actions are
  atomic and presents the same delete/create sequence as the switch operation;
- alias readback identifies exactly one target collection after the switch.

Official references:

- [Collection aliases and atomic switch](https://qdrant.tech/documentation/manage-data/collections/#collection-aliases)
- [Collection-count and multitenancy guidance](https://qdrant.tech/documentation/manage-data/collections/#setting-up-multitenancy)

The discriminating drift is scope. The alias guarantee is collection-level,
not artifact-level. It closes exact-K visibility only if the candidate
collection is a complete coherent copy of everything searched through that
alias. Reindexing one artifact in a shared namespace therefore implies a full
query-domain copy plus serialized collection cutover. Per-artifact collections
would instead require fanout/merge and incur the resource overhead Qdrant warns
about for numerous collections. Neither consequence was established by PR #452
or PR #453.

## Adversarial matrix

The real-server tests use deterministic two-dimensional DOT vectors, query
`[1, 0]`, and exact `K = 2`.

| Row state | Score posture | Required treatment |
|---|---:|---|
| old committed | current low-enough row | deliver before replacement or when unaffected |
| new staged | above current winners | never deliver before publication |
| winning current | valid current row | deliver; absence is a false negative |
| stale high score | ranks before current rows | reject without consuming K |
| losing owner | ranks before current rows | never deliver; cleanup must be owner-scoped |

The collection snapshots additionally keep one unaffected current artifact in
both old and new generations. That row catches an incomplete per-artifact copy:
switching the global alias to only the replacement artifact returns one result
instead of exact K.

## Candidate comparison

| Candidate | Quiescent safety/recall | Concurrent activation result | Boundedness | Discriminator |
|---|---|---|---|---|
| parent-head validation + iterative refill | passes on a static finite result order | fails when ranking changes between offset pages; Qdrant query pages are not claimed as one snapshot | bounded only by an explicit finite candidate budget | useful read algorithm, not a publication snapshot |
| per-chunk `published/current` flag | passes after activation is fully quiescent | deactivate-old-first creates a false-negative gap; activate-new-first exposes staged data before old is fenced | one query is bounded | no atomic multi-point activation proof |
| complete staging/live collection + alias cutover | passes | independent reader sees complete old or complete new results at exact K | one query plus one alias update | only passing global visibility seam in this bounded test |
| naive fixed 2K overfetch | fails with enough stale high scorers | fails | bounded | explicit wrong; bounded overfetch cannot guarantee recall |

The alias result is conditional on a *complete query-domain collection*. It is
not evidence that rebuilding/copying that collection is affordable, that two
artifact publications can safely build it concurrently, or that upload jobs
can durably orchestrate the copy and cutover.

## Crash, retry, readback, and cleanup evidence

The activation worker is a fresh Python process with its own Qdrant client.

| Boundary | Exercised outcome | Restart rule |
|---|---|---|
| process dies before request | alias still targets old | keep old, discard unused candidate |
| process dies after server accepted switch but before trustworthy outcome | alias targets new | exact alias readback establishes publication |
| process dies after successful readback | alias targets new | retry/readback is idempotent at the application seam |
| retry after new is already live | delete/create repeats successfully | retain new target |
| cleanup after readback | old collection deleted | alias continues returning complete new exact-K result |

“During activation” here means the client-visible ambiguous window after the
server has accepted the alias operation but before the orchestrator has a
durable outcome. The harness does **not** inject a Qdrant process, node, or
consensus crash inside the alias update. A named red and an unmarked guard make
that limitation executable; this packet makes no fabricated server-crash or
snapshot claim.

## Acceptance result

For a complete old collection and complete candidate collection, the alias
candidate satisfies both required exact-K properties under the exercised
concurrent reader:

- zero stale, staged, uncommitted, or losing-owner delivery;
- zero current false negatives whenever K current rows exist.

Every observation is one complete old or complete new snapshot and terminates
in one bounded query. The other three candidates fail a named assertion at the
boundary described above.

## Architecture decision boundary

Decision remains withheld. This spike discriminates Qdrant visibility mechanics
but does not settle the production boundary. Before any source selection, an
architecture packet still needs evidence for:

1. durable upload-to-index invocation, idempotency, retries, and visible failure;
2. construction of a complete candidate query domain without losing concurrent
   artifact publications;
3. copy cost, serialization, storage amplification, and cleanup/backpressure;
4. actual Qdrant node/cluster crash behavior if that guarantee is required;
5. reconciliation of random chunk IDs and object-id blob paths with the stated
   content-address/immutability model;
6. canonical-blob rebuild for legacy mixed-generation rows.

A passing alias test is not an architecture recommendation, source
authorization, merge authorization, or Issue #451 closure.

