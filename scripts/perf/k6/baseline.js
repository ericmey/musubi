// Baseline: single virtual user, 100 sequential requests per endpoint.
// Goal is to establish the **happy-path floor** on your hardware — the
// p50/p95/p99 you'd get if nobody else was hitting Musubi. Everything
// downstream (load, spike, soak) is judged relative to this floor.
//
// Run:
//   MUSUBI_V2_BASE_URL=... MUSUBI_V2_TOKEN=... \
//     k6 run scripts/perf/k6/baseline.js
//
// Expected runtime: ~5-10 minutes against a warm stack.

import { sleep } from 'k6';
import { retrieve, capture, sendThought, ok2xx } from './_shared.js';

export const options = {
  scenarios: {
    baseline_retrieve_fast: {
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: 100,
      exec: 'retrieveFast',
      startTime: '0s',
    },
    baseline_retrieve_deep: {
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: 100,
      exec: 'retrieveDeep',
      startTime: '120s', // after fast finishes
    },
    baseline_capture: {
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: 100,
      exec: 'capturer',
      startTime: '360s', // after deep finishes
    },
    baseline_thought: {
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: 100,
      exec: 'thinker',
      startTime: '420s',
    },
  },
  // Per-endpoint thresholds. These are the *migration-approval* budgets
  // from the perf plan — flagged as 'abortOnFail: false' so the run
  // completes + produces a full report even when something misses. Adjust
  // after Gate 1 baseline calibration.
  thresholds: {
    'http_req_duration{endpoint:retrieve_fast}': ['p(95)<500'],
    'http_req_duration{endpoint:retrieve_deep}': ['p(95)<5000'],
    'http_req_duration{endpoint:capture}': ['p(95)<1000'],
    'http_req_duration{endpoint:thoughts_send}': ['p(99)<500'],
    'http_req_failed': ['rate<0.005'],
  },
};

export function retrieveFast() {
  ok2xx(retrieve({ mode: 'fast', limit: 5 }));
  sleep(0.1); // 100ms spacing so we're measuring latency, not throughput
}

export function retrieveDeep() {
  ok2xx(retrieve({ mode: 'deep', limit: 10 }));
  sleep(0.1);
}

export function capturer() {
  ok2xx(capture());
  sleep(0.1);
}

export function thinker() {
  ok2xx(sendThought());
  sleep(0.1);
}
