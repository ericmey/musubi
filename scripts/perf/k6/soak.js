// Soak: long-duration steady-state at realistic concurrency. Goal is
// leak detection, not latency budgets — 15-minute load runs don't
// catch slow RSS drift, growing event ledger backpressure, or
// connection-pool bloat. This one runs long enough that a linear
// leak at 1 MiB/min would show up in the telemetry rollup.
//
// Shape:
//   - 2-minute ramp to SOAK_VUS (default 3 — the realistic
//     consumer concurrency; override with env)
//   - SOAK_DURATION_MINUTES (default 30) minutes steady
//   - 30-second ramp down
//
// The workload mix matches load.js (60/20/15/5 fast/deep/capture/
// thought) so we're exercising the same code paths the live fleet
// would hit, just sustained. Rate-limit budgets are relaxed vs
// baseline because the whole point is "does anything creep over
// hours", not "can we hit the happy-path floor".
//
// Run:
//   MUSUBI_V2_BASE_URL=... MUSUBI_V2_TOKEN=... \
//     k6 run scripts/perf/k6/soak.js
//
//   # Longer run for real leak hunting:
//   SOAK_DURATION_MINUTES=60 k6 run scripts/perf/k6/soak.js
//
// What to inspect after the run:
//   - ~/perf-runs/<label>/summary.md — mem_p50 / mem_max per container.
//     A clean run holds mem_p50 ≈ mem_max. Drift means a leak.
//   - GPU mem_used — TEI should stay at its warm resting VRAM.
//     Growth means model cache is expanding (shouldn't happen in prod).
//   - Qdrant segment count on the host — snapshots should be stable
//     after the first minute (aside from WAL churn).

import { sleep } from 'k6';
import { retrieve, capture, sendThought, ok2xx } from './_shared.js';

const SOAK_VUS = parseInt(__ENV.SOAK_VUS || '3', 10);
const SOAK_DURATION_MINUTES = parseInt(__ENV.SOAK_DURATION_MINUTES || '30', 10);
if (!Number.isFinite(SOAK_VUS) || SOAK_VUS < 1) {
  throw new Error('SOAK_VUS must be a positive integer; got ' + __ENV.SOAK_VUS);
}
if (!Number.isFinite(SOAK_DURATION_MINUTES) || SOAK_DURATION_MINUTES < 1) {
  throw new Error('SOAK_DURATION_MINUTES must be a positive integer; got ' + __ENV.SOAK_DURATION_MINUTES);
}

export const options = {
  scenarios: {
    soak_workload: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '2m', target: SOAK_VUS },
        { duration: `${SOAK_DURATION_MINUTES}m`, target: SOAK_VUS },
        { duration: '30s', target: 0 },
      ],
      exec: 'soakMix',
      gracefulRampDown: '30s',
    },
  },
  thresholds: {
    // Relaxed vs baseline — a few late-run stragglers shouldn't fail
    // the run when the point is leak detection.
    'http_req_duration{endpoint:retrieve_fast}': ['p(95)<800'],
    'http_req_duration{endpoint:retrieve_deep}': ['p(95)<6000'],
    'http_req_duration{endpoint:capture}': ['p(95)<1500'],
    'http_req_duration{endpoint:thoughts_send}': ['p(99)<800'],
    // Stricter failure rate — the soak shouldn't be producing 503s.
    // If it does the leak / saturation is already showing.
    'http_req_failed': ['rate<0.01'],
  },
};

export function soakMix() {
  const roll = Math.random();
  if (roll < 0.60) {
    ok2xx(retrieve({ mode: 'fast', limit: 5 }));
  } else if (roll < 0.80) {
    ok2xx(retrieve({ mode: 'deep', limit: 10 }));
  } else if (roll < 0.95) {
    ok2xx(capture());
  } else {
    ok2xx(sendThought());
  }
  sleep(0.2 + Math.random());
}
