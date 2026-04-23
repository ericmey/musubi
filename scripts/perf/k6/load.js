// Load: sustained representative workload. Weighted mix of retrieve
// (60% fast, 20% deep), capture (15%), and thoughts-send (5%).
// Holds for 13 minutes after a 2-minute ramp.
//
// Shape chosen to mimic the pessimistic case from the perf plan:
// two browser agents doing steady supplement refreshes + capture
// mirror, plus a voice agent landing intermittent bursts.
//
// LOAD_VUS (default 10) controls peak concurrency. 10 VUs is 3× the
// real 3-client concurrency ceiling and will expose the GPU saturation
// wall on reference hardware; 3 VUs is a realistic-load confirmation
// run. Use whichever matches the question you're asking.
//
// Run:
//   MUSUBI_V2_BASE_URL=... MUSUBI_V2_TOKEN=... \
//     k6 run scripts/perf/k6/load.js
//
//   # Realistic-concurrency confirmation:
//   LOAD_VUS=3 k6 run scripts/perf/k6/load.js

import { sleep } from 'k6';
import { retrieve, capture, sendThought, ok2xx } from './_shared.js';

// Strict integer parse — parseInt accepts "3.5" or "3x" and silently
// rounds down, which would let a typo quietly change peak concurrency.
const _loadVusRaw = __ENV.LOAD_VUS ?? '10';
const LOAD_VUS = Number(_loadVusRaw);
if (!Number.isInteger(LOAD_VUS) || LOAD_VUS < 1) {
  throw new Error('LOAD_VUS must be a positive integer; got ' + _loadVusRaw);
}

export const options = {
  scenarios: {
    mixed_workload: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '2m', target: LOAD_VUS },   // ramp up
        { duration: '13m', target: LOAD_VUS },  // steady
        { duration: '30s', target: 0 },         // ramp down
      ],
      exec: 'mixed',
      gracefulRampDown: '30s',
    },
  },
  thresholds: {
    // Gating budgets — perf plan §1
    'http_req_duration{endpoint:retrieve_fast}': ['p(95)<500'],
    'http_req_duration{endpoint:retrieve_deep}': ['p(95)<5000'],
    'http_req_duration{endpoint:capture}': ['p(95)<1000'],
    'http_req_duration{endpoint:thoughts_send}': ['p(99)<500'],
    'http_req_failed': ['rate<0.005'],
  },
};

// One iteration of the workload. Pacing is variable so we don't
// synchronize VUs — k6's default behaviour is round-robin which
// would make the load too uniform. `sleep` between 200ms and 1200ms
// gives realistic inter-request spacing for an LLM-driven consumer.
export function mixed() {
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
