// Shared helpers used by every scenario. Keep the imports flat and
// the logic dumb — k6 scenarios should be readable as workload
// definitions, not as bespoke client libraries.

import http from 'k6/http';
import { check } from 'k6';

// Pulled from env at scenario start so the three scenarios don't
// each duplicate the check. MUSUBI_V2_* match the env vars the voice
// and browser consumers read; same names keep the perf harness
// consistent with production.
const BASE_URL = __ENV.MUSUBI_V2_BASE_URL;
const TOKEN = __ENV.MUSUBI_V2_TOKEN;
// Canonical namespace format is `<tenant>/<presence>/<plane>` — the
// prefix supplied here MUST be exactly two segments (tenant/presence);
// the plane is appended by the call sites below. Default matches what
// `scripts/perf/seed_corpus.py` writes with its default prefix.
const NS_PREFIX = __ENV.MUSUBI_V2_NAMESPACE_PREFIX || 'perf-test/harness';

if (!BASE_URL || !TOKEN) {
  throw new Error(
    'MUSUBI_V2_BASE_URL and MUSUBI_V2_TOKEN must be set. The token ' +
    'must scope to ' + NS_PREFIX + '/* — never to eric/*.'
  );
}

// Fail fast at module load if the prefix is malformed. The server
// rejects anything other than exactly three segments with an opaque
// 500, which would otherwise mask the real problem (bad env var)
// behind a wall of failed requests mid-run.
const _nsParts = NS_PREFIX.split('/').filter(Boolean);
if (_nsParts.length !== 2) {
  throw new Error(
    'MUSUBI_V2_NAMESPACE_PREFIX must be exactly "tenant/presence" ' +
    '(two non-empty segments). Got: ' + JSON.stringify(NS_PREFIX)
  );
}

const AUTH_HEADERS = {
  'Authorization': `Bearer ${TOKEN}`,
  'Content-Type': 'application/json',
  'User-Agent': 'musubi-perf-k6/1',
};

// Five query phrases sampled by retrieve-heavy scenarios. Short,
// realistic, and intentionally thematic — they should hit rows the
// seed corpus actually seeded, so retrieves return real results
// instead of empty hits that bypass rerank.
export const QUERIES = [
  'dentist appointment Tuesday',
  'how does musubi promote concepts',
  'LiveKit voice capture pipeline',
  'what did Aoi say about the deploy',
  'thought stream SSE rules',
];

export function retrieve({ mode = 'fast', limit = 5 } = {}) {
  const q = QUERIES[Math.floor(Math.random() * QUERIES.length)];
  return http.post(
    `${BASE_URL}/retrieve`,
    JSON.stringify({
      namespace: `${NS_PREFIX}/episodic`,
      query_text: q,
      mode,
      limit,
    }),
    { headers: AUTH_HEADERS, tags: { endpoint: `retrieve_${mode}` } },
  );
}

export function capture() {
  const marker = Math.random().toString(36).slice(2, 10);
  return http.post(
    `${BASE_URL}/memories`,
    JSON.stringify({
      namespace: `${NS_PREFIX}/episodic`,
      content: `perf-capture ${marker}: Eric asked about the deploy status.`,
      tags: ['perf', 'synthetic'],
      importance: 5,
    }),
    {
      headers: { ...AUTH_HEADERS, 'Idempotency-Key': `perf-${marker}` },
      tags: { endpoint: 'capture' },
    },
  );
}

export function sendThought() {
  const marker = Math.random().toString(36).slice(2, 10);
  return http.post(
    `${BASE_URL}/thoughts/send`,
    JSON.stringify({
      namespace: `${NS_PREFIX}/thought`,
      from_presence: `${NS_PREFIX}/seeder`,
      to_presence: `${NS_PREFIX}/receiver-0`,
      content: `perf-thought ${marker}`,
      channel: 'default',
      importance: 5,
    }),
    {
      headers: { ...AUTH_HEADERS, 'Idempotency-Key': `perf-th-${marker}` },
      tags: { endpoint: 'thoughts_send' },
    },
  );
}

export function ok2xx(resp) {
  return check(resp, {
    '2xx': (r) => r.status >= 200 && r.status < 300,
  });
}
