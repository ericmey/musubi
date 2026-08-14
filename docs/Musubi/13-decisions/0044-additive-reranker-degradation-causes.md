---
title: "ADR 0044: Additive reranker degradation cause codes"
section: 13-decisions
type: adr
status: accepted
date: 2026-08-14
updated: 2026-08-14
deciders: [Eric]
tags: [architecture, retrieval, api, observability, type/adr, status/accepted]
supersedes: ""
superseded-by: ""
---

# ADR 0044: Additive reranker degradation cause codes

- **Status:** Accepted
- **Date:** 2026-08-14
- **Decider:** Eric

## Context

`reranker_failed` is a stable, truthful signal that retrieval returned fused
ranking after cross-encoder degradation. It intentionally hid implementation
detail, but hid too much: the live 413 that motivated RET-017 was publicly
indistinguishable from a timeout, an unavailable service, or malformed TEI
output. Operators had to correlate request logs to learn whether the request
was inadmissible or the service was down.

Replacing the warning would break callers that key on `reranker_failed`.
Adding arbitrary exception text would create an unbounded API and Prometheus
label surface.

## Decision

Keep `reranker_failed` and add one bounded detail code on the wire:

- `reranker_failed_timeout`
- `reranker_failed_request_rejected`
- `reranker_failed_unavailable`
- `reranker_failed_invalid_response`
- `reranker_failed_unexpected_error`

Internally, one structured warning carries `code`, `plane`, and an optional
allowlisted `cause`. Wire flattening emits the base code first and the cause
code second. Old consumers still receive the token they know; newer consumers
can distinguish the failure class without parsing prose.

The existing `musubi_retrieval_warnings_total{warning,plane}` contract remains
unchanged and counts the base warning once. Cause detail is recorded separately
as `musubi_reranker_degradation_causes_total{cause,plane}`. Neither exception
messages nor response bodies enter labels or response warnings.

## Classification

- stage-budget or HTTP-client timeout -> `timeout`
- HTTP 4xx response -> `request_rejected`
- HTTP 5xx or non-timeout network failure -> `unavailable`
- malformed JSON, shape, index, or score -> `invalid_response`
- an exception outside the typed TEI boundary -> `unexpected_error`

The classification describes the observed boundary, not root cause. A 413 is a
rejected request; it does not claim whether the client, proxy, or service owns
the mismatched ceiling.

## Consequences

- The v1 warning enum grows additively; response schemas do not change shape.
- MCP and LiveKit continue transporting warning strings and therefore preserve
  detail without adapter-specific cause translation.
- Multiple causes in one merged request may emit multiple detail codes, while
  the base warning and its legacy counter still appear once per plane.
- Free text and unknown causes fail closed at the shared retrieval boundary.

## Rejected alternatives

- **Replace `reranker_failed`:** breaks existing consumers and dashboards.
- **Expose HTTP status or exception text:** unbounded, potentially sensitive,
  and not stable across proxies or client libraries.
- **Add `cause` to the existing warning metric:** changes a frozen label set
  and fragments the established base-warning time series.
