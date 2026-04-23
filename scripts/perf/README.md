# Musubi perf testing harness

Load + performance testing scripts for `musubi.mey.house`. Implements
the plan at *(link in PR description)*.

## Layout

```
scripts/perf/
├── README.md                  ← you are here
├── seed_corpus.py             ← deterministic synthetic corpus generator
├── telemetry.sh               ← host-side resource sampler (docker stats + GPU)
└── k6/
    ├── _shared.js             ← shared request builders (auth, endpoints)
    ├── baseline.js            ← single-VU floor measurement
    ├── load.js                ← sustained mixed workload
    └── spike.js               ← voice-burst-on-top-of-background
```

## Prerequisites

1. **Operator token scoped to `perf-test/harness/*`** (or whichever
   two-segment prefix you override to) — *never* use `eric/*`. The
   scope list must include `operator`, per-plane `<prefix>/<plane>:rw`,
   and `thoughts:send`. See [.agent-context.local.md](../../.agent-context.local.md)
   for how to mint a scoped token against the running stack's
   `JWT_SIGNING_KEY`.
2. **k6 installed** on whatever host runs the scenarios
   (`brew install k6` on macOS, apt package on Ubuntu).
3. **jq** (telemetry summarizer uses it).
4. **Python 3.12 + httpx** for the seed script
   (`pip install httpx`).
5. **A named pre-run snapshot** (for safety — see
   [deploy/runbooks/manual-recovery.md](../../deploy/runbooks/manual-recovery.md)).

## Env vars every scenario reads

```bash
export MUSUBI_V2_BASE_URL=http://musubi.mey.house:8100/v1
export MUSUBI_V2_TOKEN=mbi_perf_...                        # scoped to the prefix below
export MUSUBI_V2_NAMESPACE_PREFIX=perf-test/harness        # tenant/presence — plane is appended
```

The prefix must be exactly two segments — the server's namespace
regex requires `tenant/presence/plane` (three segments total), and
the seed script + k6 scenarios append the plane automatically. A
one-segment prefix like `perf-test` will produce invalid namespaces
and the server rejects them with a 500.

## Typical run sequence (per the plan)

### Gate 1 — rebaseline (single caller)

```bash
make perf-seed SIZE=10000 SEED=42                  # one-time
make perf-baseline LABEL=rebaseline-$(date +%Y%m%d)
```

Produces `~/perf-runs/rebaseline-<date>/` with k6 summary + telemetry rollup.

### Gate 2 — load

```bash
# Default: 10 VUs (3× realistic concurrency — probes the GPU wall)
make perf-load LABEL=load-$(date +%Y%m%d)

# Realistic-concurrency confirmation (matches 2 browser + 1 voice agents)
LOAD_VUS=3 make perf-load LABEL=load-3vu-$(date +%Y%m%d)
```

### Gate 3 — spike

```bash
make perf-spike LABEL=spike-$(date +%Y%m%d)
```

### Gate 4 — soak

```bash
# Default: 3 VUs, 30 min — enough to catch a linear leak at ~1 MiB/min.
make perf-soak LABEL=soak-$(date +%Y%m%d)

# Longer run (overnight or hunting a slow drift):
SOAK_DURATION_MINUTES=120 make perf-soak LABEL=soak-long-$(date +%Y%m%d)
```

## What each script does (one-liner)

- **`seed_corpus.py`** — POSTs N rows to each plane's canonical
  endpoint with deterministic content sampled from a seeded
  fragment pool. Idempotent: re-running with the same `--seed` is a
  no-op against a populated namespace (dedup + idempotency keys
  collapse duplicate captures).
- **`k6/baseline.js`** — serial happy-path. Per-endpoint p50/p95/p99.
- **`k6/load.js`** — default 10 VUs, ramp + steady 15 min, weighted mix.
  Peak concurrency is configurable via `LOAD_VUS` (e.g. `LOAD_VUS=3`
  for a realistic-load confirmation run).
- **`k6/spike.js`** — background at 2 RPS, burst to 15 RPS for 20 s,
  recovery window.
- **`k6/soak.js`** — leak detection at realistic concurrency. Default
  3 VUs × 30 min; override with `SOAK_VUS` / `SOAK_DURATION_MINUTES`.
  Fail budgets are the same as load (0.005) — a clean soak should not
  produce 503s.
- **`telemetry.sh`** — sidecar sampler. Runs alongside k6 and
  captures container CPU/RSS + GPU utilization + memory at 5s cadence.
  When the driver runs on a different host than Musubi (common), set
  `REMOTE_HOST=<user>@<musubi-host>` to sample over SSH. If the SSH
  user isn't in the host's `docker` group but has passwordless sudo,
  set `REMOTE_DOCKER_PREFIX="sudo -n"` — without it the remote
  `docker stats` call fails silently and you end up with an empty
  jsonl after the run.

## Output

Every Makefile target writes to `~/perf-runs/<LABEL>/`:

```
~/perf-runs/rebaseline-20260422/
├── k6-summary.json          ← k6's built-in summary
├── docker-stats.jsonl       ← per-container samples
├── nvidia-smi.jsonl         ← GPU samples (if NVIDIA present)
└── summary.md               ← rollup (run `telemetry.sh summarize <label>`)
```

## Safety

- **Never point scenarios at `eric/*` namespaces.** The seed script
  + k6 scenarios all default to `perf-test/*` and error if
  `MUSUBI_V2_TOKEN` is unset. If the token grants access to
  `eric/*` you're one config-flag away from corrupting live data.
  Mint the token with the narrow scope.
- **Have a rollback point.** Take a labeled snapshot before each
  gate per [manual-recovery.md](../../deploy/runbooks/manual-recovery.md).
- **Watch GPU VRAM.** The RTX 3080 has 10 GiB shared between
  TEI-dense, TEI-sparse, BGE-reranker, and Ollama. Perf gate says
  keep ≥ 1 GiB free; the telemetry summary flags it explicitly.

## Related

- Plan: see Gate 0 PR description for the full test plan.
- Follow-up: [#190](https://github.com/ericmey/musubi/issues/190) —
  restore.yml repair (independent, not blocking).
