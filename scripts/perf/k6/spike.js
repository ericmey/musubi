// Spike: voice-call burst on top of background steady-state. Mimics
// the pessimistic case — Eric drops into a LiveKit call while the
// browser agents keep doing their thing.
//
// Shape (per the perf plan):
//   - 2 min at 2 RPS "background" (simulates browser + idle voice)
//   - 20 seconds at 15 RPS "in-call" (voice turns firing tools)
//   - 1 min at 2 RPS "recovery" (does p95 settle back to baseline?)
//
// Recovery measurement is the point. A system that can handle steady
// state but can't recover from a burst looks fine on dashboards
// between calls and catastrophic during one.
//
// Run:
//   MUSUBI_V2_BASE_URL=... MUSUBI_V2_TOKEN=... \
//     k6 run scripts/perf/k6/spike.js

import { sleep } from 'k6';
import { retrieve, capture, sendThought, ok2xx } from './_shared.js';

export const options = {
  scenarios: {
    spike_workload: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      preAllocatedVUs: 30,
      maxVUs: 50,
      stages: [
        { duration: '30s', target: 2 },   // ramp to background
        { duration: '90s', target: 2 },   // steady 2 RPS
        { duration: '2s', target: 15 },   // burst start
        { duration: '20s', target: 15 },  // peak 15 RPS (in-call)
        { duration: '2s', target: 2 },    // drop off
        { duration: '60s', target: 2 },   // recovery window
      ],
      exec: 'spikeMix',
    },
  },
  thresholds: {
    'http_req_duration{endpoint:retrieve_fast}': ['p(95)<800'],  // relaxed during spike
    'http_req_duration{endpoint:retrieve_deep}': ['p(95)<6000'], // relaxed during spike
    'http_req_failed': ['rate<0.02'],  // some 5xx acceptable mid-burst
  },
};

// Spike workload leans retrieve-heavy (80%) — voice tools hit recall
// more than remember/think per turn. Capture + thoughts still present
// because a real call does include "remember that" and "tell X"
// requests.
export function spikeMix() {
  const roll = Math.random();
  if (roll < 0.65) {
    ok2xx(retrieve({ mode: 'fast', limit: 5 }));
  } else if (roll < 0.80) {
    ok2xx(retrieve({ mode: 'deep', limit: 10 }));
  } else if (roll < 0.92) {
    ok2xx(capture());
  } else {
    ok2xx(sendThought());
  }
  sleep(0.1);
}
