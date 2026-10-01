---
title: Capacity Planning
section: 09-operations
tags: [capacity, operations, scale, section/operations, status/draft, type/runbook]
type: runbook
status: draft
updated: 2026-10-01
up: "[[09-operations/index]]"
reviewed: false
---
# Capacity Planning

What size is Musubi? When does it outgrow one box? What signal tells you before it does?

All hardware figures refer to the measured reference host (Ryzen 5, 32 GB RAM, RTX 3080
10 GB, NVMe; see [[08-deployment/host-profile]]) running the GPU overlay. **Most numbers
on this page are planning estimates that have not been re-measured against the current
release;** each table says which.

## v1 scope

Small team / single operator:

- **Users:** 1-5 humans, 3-10 agent presences.
- **Captures:** 100-5,000 / day.
- **Retrievals:** 500-10,000 / day (voice sessions are retrieval-heavy; coding sessions
  bursty).
- **Curated docs:** ~500-5,000 long-term.
- **Concepts:** ~500 active at steady state.
- **Artifacts:** ~1,000-10,000 / year.

## Resource footprint

Planning estimates (unverified) for ~5 presences, ~2k captures/day, ~5k retrievals/day:

| Resource | Idle | Typical | Peak |
|---|---|---|---|
| CPU | 5% | 15% | 60% (synthesis batch) |
| RAM | 8 GB | 14 GB | 22 GB |
| VRAM | see below | ~9 GB | ~9.5 GB |
| Disk (write) | negligible | few MB/min | ~50 MB/min (batch) |

The one VRAM measurement on record: about 3.3 GiB in use on the reference host with all
four GPU services running and idle (whether the LLM was resident was not recorded). The
budget in [[08-deployment/gpu-inference-topology]] assumes ~9 GB with everything loaded.

## Growth model

Planning estimates (unverified), steady state:

| Store | Rate | Year 1 | Year 3 | Year 5 |
|---|---|---|---|---|
| Episodic (Qdrant) | 2 GB/yr | 2 GB | 6 GB | 10 GB |
| Curated (vault) | 50 MB/yr | 50 MB | 150 MB | 250 MB |
| Concepts (Qdrant) | 100 MB/yr | 100 MB | 300 MB | 500 MB |
| Artifact blobs | 10-30 GB/yr | 20 GB | 60 GB | 100 GB |
| Artifact chunks (Qdrant) | 5 GB/yr | 5 GB | 15 GB | 25 GB |
| Lifecycle sqlite | 500 MB/yr | 500 MB | 1 GB | 2 GB |

Year-5 total: roughly 150 GB of live data, plus whatever backup sets and snapshots you
keep on the same disk. Artifact blobs dominate growth.

## Retrieval and capture throughput

Planning estimates (unverified):

- **Fast retrieval:** ~150 req/s sustained (GPU encoders are the limit).
- **Deep retrieval:** ~10 req/s sustained (reranker-bound).
- **Single capture:** ~50 ms p95 (dense + sparse encode, Qdrant write, dedup probe).
- **Sustained capture:** ~50/s; batch endpoint higher.

v1 workload is ~2k captures/day (0.02/s average): orders of magnitude of headroom.

## Scale signals: when to worry

Measure these with your own monitoring:

- Disk holding Docker's volumes > 75% full (host exporter).
- VRAM near the card's limit for 10+ minutes (GPU exporter; Core emits no GPU metrics).
- Retrieval p95 above 500 ms, sustained:
  `histogram_quantile(0.95, sum by (le) (rate(musubi_http_request_duration_ms_bucket{endpoint="/v1/retrieve"}[5m])))`.
- `musubi_lifecycle_job_duration_seconds` for a job approaching its schedule interval.

Any of these means it is time to think about scaling.

## Scaling options (in order of effort)

### 1. Tune the current box

- Raise TEI `--max-batch-tokens` if the GPU has headroom (overlay `command:` lines).
- Prune episodic memories through the demotion rules ([[06-ingestion/demotion]]).
- HNSW and quantization settings are fixed at collection creation
  ([[08-deployment/qdrant-config]]); changing them means rebuilding a collection.

### 2. Add or upgrade a GPU

Put the LLM on a second or larger card; the encoders stay where they are.

### 3. Move the LLM off-box

Run Ollama on a second host with a larger GPU and point `OLLAMA_URL` at it
(`LIFECYCLE_LLM_*` can move maturation and synthesis to any OpenAI-compatible endpoint,
but promotion and reflection always use `OLLAMA_URL`). Keep the encoders close to Core:
they are on the
capture and retrieval path.

### 4. Move Qdrant to a separate host

Not supported by the shipped stack, which runs Qdrant in the same Compose project. This
is multi-host migration work, outside v1 scope.

### 5. Cluster Qdrant

Qdrant supports sharding and replication. Only needed far beyond v1 scope.

## LLM capacity

Planning estimate (unverified): Qwen 3 4B Q4 on a 3080 generates tens of tokens per
second. At ~50 synthesis candidates and ~5 promotions a day, the LLM is busy for minutes
a day, not hours. The LLM never runs on the capture or retrieval path.

## Request rate limits vs capacity

Core rate-limits **write-method** requests (POST, PUT, PATCH, DELETE) per token, per
minute (`src/musubi/api/rate_limit.py`):

| Bucket | Limit / min |
|---|---|
| `capture` (episodic and curated writes) | 100 |
| `thought` | 100 |
| `artifact-upload` | 20 |
| `batch-write` | 50 |
| `transition` | 50 |
| `default` (everything else, including `POST /v1/retrieve` and `/v1/context`) | 200 |

Operator-scoped tokens get 10x. GET requests are not rate-limited. Any edge rate limit in
your reverse proxy is your own setting. The limits are there to stop runaway clients, not
to manage capacity.

## Cost of running

Self-hosted on owned hardware, the marginal costs are electricity, off-host backup
storage, and a domain and certificate if you expose Core. Compare against a hosted
vector DB plus LLM API plus storage at similar throughput.

## Forecasting

If disk growth runs above projection by 2x for more than two weeks, investigate. Common
causes:

- A presence capturing too aggressively.
- Chunker output larger than expected.
- Very large artifact uploads.

## Test contract

**Module under test:** capacity math and thresholds.

No automated test covers this page. Load and growth tests (retrieval p95 at 150 req/s,
capture p95 at 50 req/s, synthesis time on 50 candidates, storage growth) are planned,
not implemented.
