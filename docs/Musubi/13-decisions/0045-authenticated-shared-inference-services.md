---
title: "ADR 0045: Authenticated shared inference services"
section: 13-decisions
type: adr
status: accepted
date: 2026-09-19
updated: 2026-09-20
deciders: [Eric]
tags: [architecture, inference, security, deployment, type/adr, status/accepted]
supersedes: ""
superseded-by: ""
---

# ADR 0045: Authenticated shared inference services

- **Status:** Accepted
- **Date:** 2026-09-19
- **Decider:** Eric

## Context

Musubi currently owns three TEI containers inside its application Compose
stack: BGE-M3 dense embeddings, SPLADE-v3 sparse embeddings, and the BGE-M3
reranker. Other services cannot consume them without making Musubi Core or
LiteLLM an accidental inference gateway. Chord needs dense embeddings, and Eric
decided that the models should become independently managed services on the
Musubi inference host so other services can reuse them.

TEI has no native authentication. A previous design published a raw TEI port
and attempted to restrict it with UFW/`DOCKER-USER` rules. That design was
withdrawn after proving two fail-open cutover paths. Docker-published traffic is
DNATed through `FORWARD`; an `INPUT` rule can report success while protecting
nothing. The current Compose stack is safer: none of the TEI services has a
`ports:` entry, so no raw attack surface exists.

The host cannot run a second copy of all three models during migration. The
cutover therefore needs a bounded handoff and a tested rollback, not a
blue/green assumption the GPU cannot satisfy.

## Decision

Extract all three TEI services into a dedicated Compose file and a dedicated
systemd unit. Musubi application lifecycle operations must not stop or recreate
the shared inference unit.

The raw TEI services remain private to a Docker network and carry no host
`ports:` entries. Exactly one service may publish a host port: a thin inference
ingress. The safe test is an allowlist of publishers, not a roster of services
expected to remain private. A newly added publisher therefore fails closed.

The ingress authenticates every dense, sparse, and reranker request. Musubi and
Chord receive distinct credentials from 1Password. The credentials exist for
independent revocation, independent rotation, caller attribution, and bounded
per-consumer accounting; TEI is stateless and the separation is not described
as corpus isolation.

Consumer credentials are carried in the HTTP `Authorization` header, never in
endpoint URL userinfo. Request libraries routinely include URLs in access logs,
exceptions, retry diagnostics, and request representations; a secret embedded
in a URL therefore becomes log data even when the ingress itself is correctly
configured not to log payloads.

External consumers reach the authenticated ingress through the fleet TLS
boundary. Musubi may use the same ingress over the host-private Docker network.
No consumer calls a raw TEI container, and Chord does not route embeddings
through LiteLLM or the Musubi API.

The ingress does not log request bodies or body hashes. Operational logging is
limited to caller identity, route, status, latency, and byte counts. The ingress
sees both tenants' request text in transit; retaining payload-derived material
would create a cross-tenant correlation store even though TEI itself retains no
corpus.

The OpenAI-shaped Chord `/v1/embeddings` surface uses BGE-M3 dense output only.
SPLADE-v3 and the reranker remain shared inference capabilities for consumers
whose contracts match their output; they are not coerced into
`CreateEmbeddingResponse`.

## Migration and rollback invariants

1. Never add a temporary raw TEI `ports:` entry. The no-publish property remains
   true throughout migration, so its check is never disabled and cannot be
   forgotten afterward.
2. Render and validate the new deployment before stopping the old owner.
3. Stop the old TEI owner only after the new ingress, credentials, model volume,
   and rollback material are present.
4. Verify authenticated parity for all three routes before cutting Musubi's
   URLs over.
5. If startup or parity fails, restore the previous Compose ownership and URLs;
   a failed migration must not leave an unauthenticated or half-managed endpoint.
6. Preserve the existing model volume. Model extraction changes lifecycle
   ownership, not model identity or downloaded bytes.
7. Rotate an exposed consumer credential transactionally: install old and new
   ingress identities together, prove both, restart the real consumer on the
   new header credential, remove the old identity, and prove the old identity
   returns `401`. Any failure restores the previous client references, ingress
   password file, and running consumer before refusing the migration.
8. Prometheus remains attached to the raw inference network solely for service
   monitoring. Application consumers remain ingress-only. The network sets are
   asserted exactly so restoring TEI monitoring cannot accidentally give Core
   or lifecycle direct backend access.

## Consequences

- Musubi deployment gains a second independently managed unit and an explicit
  dependency on its healthy ingress.
- Chord can implement embeddings against a direct inference contract without
  depending on Musubi Core or LiteLLM.
- Credential rotation can disable one consumer without disrupting the other.
- The single-host GPU imposes a short, controlled model-owner handoff during the
  first extraction; subsequent Musubi application deploys do not restart TEI.
- Live addresses and credentials remain private inventory. Public templates and
  tests use symbolic values or documentation-only addresses.

## Rejected alternatives

- **Publish raw TEI and filter by source IP:** the control is easy to attach to
  the wrong netfilter path and does not scale to multiple consumers.
- **Expose TEI directly on the VLAN:** unauthenticated model execution becomes a
  network-wide capability.
- **Proxy through Musubi Core:** preserves the lifecycle coupling this decision
  exists to remove and makes Musubi an inference gateway.
- **Route through LiteLLM:** violates the direct-to-service measurement and
  ownership boundary Eric set for Chord.
- **Run duplicate stacks for a zero-downtime cutover:** the host does not have
  the GPU headroom to make that an honest deployment plan.

## Test contract

1. `test_shared_inference_is_owned_by_a_separate_deployment_unit`
2. `test_tei_backends_are_not_host_published`
3. `test_every_shared_endpoint_requires_authentication`
4. `test_consumers_receive_distinct_runtime_credentials`
5. `test_failed_cutover_keeps_the_old_authenticated_endpoint_protected`
6. `test_live_values_do_not_enter_public_sources`
7. `test_auth_rotation_rolls_back_every_coupled_artifact`
8. `test_prometheus_is_the_only_musubi_service_on_the_backend_network`
