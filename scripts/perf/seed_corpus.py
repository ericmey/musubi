#!/usr/bin/env python3
"""Seed a Musubi instance with a deterministic synthetic corpus for
perf testing.

Philosophy
----------
Perf runs need to be **reproducible across runs**. That means the
corpus seeded before Gate 2 (load) must be bit-identical to the corpus
seeded before Gate 4 (reliability), so latency deltas between runs
reflect code/hardware changes, not corpus drift. We drive everything
from a single ``--seed`` argument — the random selection, the length
distribution, the topic tagging, the timestamp spread. Same seed =
same corpus.

Philosophy (the other part)
---------------------------
We deliberately do **not** hit an external LLM (Ollama, OASST) for
generation. That keeps the seed step self-contained and deterministic
— no "but the model version changed" variance. The pool of seed
fragments is intentionally mundane household-conversation-shaped text;
the point is exercising dense/sparse embedding + rerank latency, not
producing human-realistic corpora.

Targets
-------
Seeds four of the five planes via Musubi's canonical API:

  * episodic   — ``POST /v1/episodic``
  * curated    — ``POST /v1/curated`` (via operator token)
  * artifact   — ``POST /v1/artifacts`` (multipart)
  * thought    — ``POST /v1/thoughts/send``

The **concept** plane is intentionally omitted: there is no
``POST /v1/concepts`` endpoint — concepts are produced only by the
lifecycle synthesis job against episodic clusters. If you need the
concept plane populated for a retrieval test, either run the real
synthesis sweep after seeding episodic, or call the
debug-synthesis trigger.

Namespaces are pinned under ``<tenant>/<presence>/<plane>`` (the
canonical three-segment format), defaulting to
``perf-test/harness/<plane>`` so live data at ``eric/*`` is never
touched. The tenant + presence are taken from ``--namespace-prefix``
which must be exactly two segments.

Usage
-----
  MUSUBI_V2_BASE_URL=http://musubi.mey.house:8100/v1 \\
  MUSUBI_V2_TOKEN=mbi_perf_... \\
  python3 scripts/perf/seed_corpus.py \\
      --size 10000 --seed 42 --namespace-prefix perf-test/harness

Size is per-plane; ``--size 10000`` creates 10k episodic + 10k curated
+ 10k concept + 10k artifact + 10k thought. Use ``--planes`` to limit.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

try:
    import httpx
except ImportError:
    sys.stderr.write("seed_corpus.py requires httpx. Install: pip install httpx\n")
    sys.exit(2)

# Fixed anchor — timestamps derive as `TIMESTAMP_ANCHOR - offset_seconds`
# where offset comes from the seeded RNG. Pinning this to a constant
# epoch makes the entire corpus — content + timestamps + idempotency
# keys — bit-identical across runs of the same --seed. That's what
# makes the seed genuinely idempotent on retry: the server sees the
# same idempotency key and dedupes into the pre-existing row.
TIMESTAMP_ANCHOR = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)

log = logging.getLogger("musubi.perf.seed")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# Note: concept is intentionally absent. Concepts are produced by the
# lifecycle synthesis job against episodic clusters — there is no
# direct POST /v1/concepts endpoint (it returns 405), so seeding them
# via the HTTP API is impossible. Either run the real synthesis job
# after seeding episodic, or hit the debug-synthesis trigger.
PLANES = ("episodic", "curated", "artifact", "thought")

# Intentionally mundane fragments — household + ops + meta mix. The
# pool is small on purpose: seeded random sampling over a small pool
# gives realistic duplication + reinforcement patterns (the same fact
# mentioned in two episodic captures) which exercises dedup + scoring.
_FRAGMENTS: tuple[str, ...] = (
    "Remember the dentist appointment on Tuesday afternoon.",
    "Eric prefers coffee black, no sugar.",
    "Aoi mentioned the deploy finished cleanly.",
    "Nyla is running a background sweep on household memory.",
    "Party agent handles delegation to the other voice agents.",
    "OpenClaw sidecar is responsible for capture mirroring.",
    "The LiveKit stack routes SIP into voice tools.",
    "Kong is the gateway that fronts musubi.mey.house for external traffic.",
    "Qdrant holds every plane's vector embeddings behind named collections.",
    "TEI serves BGE-M3 dense + SPLADE sparse embeddings on the RTX 3080.",
    "BGE-reranker-v2-m3 does the cross-encoder rerank pass on hybrid retrieve.",
    "Ollama runs Qwen2.5-7B Q4 for importance scoring and fact extraction.",
    "Musubi's lifecycle engine sweeps maturation every five minutes.",
    "Concept synthesis kicks off when episodic cluster size crosses threshold.",
    "Promotion moves a concept into the curated plane after reinforcement.",
    "Demotion archives episodic rows past the provisional TTL.",
    "Reflection runs weekly and looks for contradiction patterns.",
    "The auto-digest-bump workflow opens a PR on every release:published.",
    "cosign keyless signs the core image via GitHub OIDC.",
    "CycloneDX SBOM is attached to the image via anchore/sbom-action.",
    "Trivy scans each published image and uploads SARIF to Code Scanning.",
    "Slice-adapter-livekit shipped end-to-end tests against docker-compose Musubi.",
    "The thought stream SSE consumer honors six consumer-expectation rules.",
    "Presence resolution maps OpenClaw agent ids to Musubi presences.",
    "Bearer tokens scope per-namespace read/write on the canonical API.",
    "The curated plane is read-mostly; vault sync is the writer.",
    "Episodic memories land provisional and mature to a stable state.",
    "Retrieve fast-mode returns in under 200ms on reference hardware.",
    "Retrieve deep-mode budgets five seconds for the full hybrid path.",
    "Thoughts flow presence-to-presence with optional channel routing.",
    "A vault sync pulls curated markdown into the episodic/concept chunker.",
    "The observability stack emits structured JSON one field per concept.",
    "Hybrid retrieve blends dense + sparse + lexical + recency + importance.",
    "Namespace scope errors surface as 403 with a typed error envelope.",
    "Rate limits follow the token-bucket pattern with operator multiplier.",
    "Idempotency keys dedupe retried writes so the same capture is one row.",
    "Artifact plane stores content-addressed blobs plus chunk embeddings.",
    "Migration slice ETLs the alpha POC into the new canonical layout.",
    "The release-please workflow cuts semver releases from conventional commits.",
    "Every plane carries bitemporal event_at plus ingested_at timestamps.",
    "Lineage fields supersedes and superseded_by chain mutations explicitly.",
    "KSUID object ids live in payload; Qdrant point ids stay UUID.",
    "Schema version is stamped on every payload for forward-read compatibility.",
    "Operator notes flag anything sensitive that shouldn't land in git.",
    "The scheduler channel carries time-boxed reminders between agents.",
    "musubi_recall hits deep-mode retrieve across every plane by default.",
    "musubi_remember captures at importance seven, above the passive baseline.",
    "musubi_think delivers a presence-to-presence thought inline from voice.",
    "A voice call triggers bursty retrieve+capture for a couple minutes.",
    "Soak tests catch leaks that 15-minute load runs miss entirely.",
    "Spike tests mimic a LiveKit session landing on top of background load.",
)

_TOPICS: tuple[str, ...] = (
    "household",
    "deploy",
    "ops",
    "voice",
    "memory",
    "retrieval",
    "lifecycle",
    "security",
    "observability",
    "migration",
    "scheduler",
    "presence",
)


@dataclass(frozen=True)
class SeedConfig:
    base_url: str
    token: str
    size: int
    seed: int
    namespace_prefix: str
    planes: tuple[str, ...]
    # How far back to spread timestamps. 90 days gives maturation a
    # realistic age distribution (some provisional, some matured).
    timespan_days: int = 90
    # Per-request timeout. Generous — the seed runs once per corpus,
    # we care about completing, not latency.
    request_timeout_s: float = 30.0


def parse_args() -> SeedConfig:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--size", type=int, required=True, help="rows per plane")
    p.add_argument("--seed", type=int, default=42, help="deterministic RNG seed")
    p.add_argument(
        "--namespace-prefix",
        default="perf-test/harness",
        help=(
            "tenant/presence prefix for all seeded data; the plane is "
            "appended automatically to produce the canonical three-segment "
            "namespace. Kept off 'eric/*' on purpose."
        ),
    )
    p.add_argument(
        "--planes",
        default=",".join(PLANES),
        help=f"comma-separated subset of {PLANES}",
    )
    p.add_argument("--timespan-days", type=int, default=90)
    args = p.parse_args()

    base_url = os.environ.get("MUSUBI_V2_BASE_URL")
    token = os.environ.get("MUSUBI_V2_TOKEN")
    if not base_url or not token:
        sys.stderr.write(
            "error: MUSUBI_V2_BASE_URL and MUSUBI_V2_TOKEN must be set.\n"
            "       The token must scope write access on "
            f"{args.namespace_prefix}/* — never on eric/*.\n"
        )
        sys.exit(2)

    planes = tuple(p.strip() for p in args.planes.split(",") if p.strip())
    bad = [p for p in planes if p not in PLANES]
    if bad:
        sys.stderr.write(f"error: unknown plane(s): {bad}\n")
        sys.exit(2)

    # Server rejects anything other than exactly `<tenant>/<presence>/<plane>`
    # — fail loudly here instead of eating 500s mid-run.
    prefix = args.namespace_prefix.strip("/")
    if prefix.count("/") != 1 or not all(prefix.split("/")):
        sys.stderr.write(
            f"error: --namespace-prefix must be 'tenant/presence' (got {prefix!r}).\n"
            "       The plane is appended automatically.\n"
        )
        sys.exit(2)

    return SeedConfig(
        base_url=base_url.rstrip("/"),
        token=token,
        size=args.size,
        seed=args.seed,
        namespace_prefix=prefix,
        planes=planes,
        timespan_days=args.timespan_days,
    )


def make_content(rng: random.Random) -> str:
    """Pick 1-4 fragments and join them. Seeded sampling from a small
    pool produces realistic duplication and reinforcement patterns."""
    k = rng.randint(1, 4)
    return " ".join(rng.sample(_FRAGMENTS, k=k))


def make_timestamp(rng: random.Random, timespan_days: int) -> datetime:
    """Uniform spread across ``timespan_days`` ending at the fixed
    ``TIMESTAMP_ANCHOR``. Using a fixed anchor (not ``datetime.now()``)
    is what lets the seed script be genuinely idempotent on retry:
    same --seed produces the same timestamps, hence the same
    idempotency keys, hence the server dedupes retried writes into
    the existing rows instead of creating duplicates."""
    seconds = rng.randint(0, timespan_days * 86400)
    return TIMESTAMP_ANCHOR - timedelta(seconds=seconds)


def make_idempotency_key(namespace: str, content: str, timestamp: datetime) -> str:
    """Deterministic idempotency key. Re-running the seed against the
    same Musubi should NOT create duplicates — Musubi's capture pipeline
    dedups on this key."""
    h = hashlib.sha256()
    h.update(namespace.encode())
    h.update(content.encode())
    h.update(timestamp.isoformat().encode())
    return f"perf-seed:{h.hexdigest()[:24]}"


# Backoff tuning. Musubi's rate-limit middleware returns 429 with a
# Retry-After header (in seconds) — honor it first. If the header
# is missing, fall back to exponential backoff anchored at 250ms with
# jitter, capped at MAX_BACKOFF_S. After MAX_429_RETRIES attempts we
# give up on the row and move on — a dropped seed row is recoverable
# by re-running (idempotency keys dedupe against what's already there).
MAX_429_RETRIES = 6
BASE_BACKOFF_S = 0.25
MAX_BACKOFF_S = 30.0


def _sleep_for_429(resp: httpx.Response, attempt: int, rng: random.Random) -> float:
    """Return how long we slept, for observability + tests."""
    retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    if retry_after:
        try:
            wait_s = float(retry_after)
        except ValueError:
            # Retry-After can be an HTTP date; we don't need that
            # branch for Musubi (middleware emits integer seconds).
            wait_s = BASE_BACKOFF_S * (2**attempt)
    else:
        # 0.25s, 0.5s, 1s, 2s, 4s, 8s + 0-25% jitter.
        wait_s = BASE_BACKOFF_S * (2**attempt)
        wait_s += wait_s * rng.random() * 0.25
    # Clamp below too — a misbehaving proxy returning a negative
    # Retry-After would otherwise crash time.sleep().
    wait_s = max(0.0, min(wait_s, MAX_BACKOFF_S))
    time.sleep(wait_s)
    return wait_s


def post_with_backoff(
    client: httpx.Client,
    path: str,
    *,
    rng: random.Random,
    json_body: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response | None:
    """POST wrapper that retries 429s with Retry-After / exp-backoff.

    Returns the final ``httpx.Response`` on 2xx/4xx-other, or None if
    we hit ``MAX_429_RETRIES`` consecutive 429s (caller logs + moves
    on). Non-429 4xx and 5xx are NOT retried — those are either client
    bugs or transient server errors that the outer loop's per-row
    warning surfaces. Keeps the seed simple: dedup makes re-runs
    the retry mechanism for flaky rows."""
    for attempt in range(MAX_429_RETRIES):
        try:
            r = client.post(
                path,
                json=json_body,
                data=data,
                files=files,
                headers=extra_headers,
            )
        except httpx.HTTPError as exc:
            log.warning("POST %s transport error (attempt %d): %s", path, attempt, exc)
            return None
        if r.status_code != 429:
            return r
        slept = _sleep_for_429(r, attempt, rng)
        log.info("429 on %s (attempt %d); slept %.2fs", path, attempt, slept)
    log.warning("POST %s gave up after %d consecutive 429s", path, MAX_429_RETRIES)
    return None


def seed_episodic(client: httpx.Client, cfg: SeedConfig, rng: random.Random) -> int:
    """POST /v1/episodic. Repeated calls with the same idempotency key
    fold into a single row, so the seed is safe to retry."""
    ns = f"{cfg.namespace_prefix}/episodic"
    ok = 0
    progress_rng = random.Random(cfg.seed ^ 0xBEEF)  # separate stream for sleeps
    for i in range(cfg.size):
        content = make_content(rng)
        ts = make_timestamp(rng, cfg.timespan_days)
        body = {
            "namespace": ns,
            "content": content,
            "tags": rng.sample(_TOPICS, k=rng.randint(1, 3)),
            "topics": rng.sample(_TOPICS, k=rng.randint(0, 2)),
            "importance": rng.randint(1, 9),
        }
        r = post_with_backoff(
            client,
            "/memories",
            rng=progress_rng,
            json_body=body,
            extra_headers={"Idempotency-Key": make_idempotency_key(ns, content, ts)},
        )
        if r is not None and r.is_success:
            ok += 1
        elif r is not None:
            log.warning("episodic %d: %d %s", i, r.status_code, r.text[:160])
        else:
            log.warning("episodic %d: skipped (transport error or 429 exhaustion)", i)
        if i and i % 500 == 0:
            log.info("episodic: %d / %d (ok=%d)", i, cfg.size, ok)
    return ok


def seed_thought(client: httpx.Client, cfg: SeedConfig, rng: random.Random) -> int:
    ns = f"{cfg.namespace_prefix}/thought"
    ok = 0
    backoff_rng = random.Random(cfg.seed ^ 0xF00D)
    for i in range(cfg.size):
        body = {
            "namespace": ns,
            "from_presence": f"{cfg.namespace_prefix}/seeder",
            "to_presence": f"{cfg.namespace_prefix}/receiver-{i % 4}",
            "content": make_content(rng),
            "channel": rng.choice(("default", "scheduler", "ops")),
            "importance": rng.randint(1, 9),
        }
        r = post_with_backoff(client, "/thoughts/send", rng=backoff_rng, json_body=body)
        if r is not None and r.is_success:
            ok += 1
        elif r is not None:
            log.warning("thought %d: %d %s", i, r.status_code, r.text[:160])
        else:
            log.warning("thought %d: skipped (transport error or 429 exhaustion)", i)
        if i and i % 500 == 0:
            log.info("thought: %d / %d (ok=%d)", i, cfg.size, ok)
    return ok


def seed_curated(client: httpx.Client, cfg: SeedConfig, rng: random.Random) -> int:
    """POST /v1/curated. Requires operator scope + body_hash.
    Vault sync is the normal writer — seeding here is a deliberate
    bypass for perf-testing corpus construction only.

    Idempotency-on-retry: the ``body_hash`` is a pure function of
    ``content``, so re-running the same --seed produces the same hashes
    and the server dedupes into existing rows."""
    ns = f"{cfg.namespace_prefix}/curated"
    ok = 0
    backoff_rng = random.Random(cfg.seed ^ 0xCAFE)
    for i in range(cfg.size):
        content = make_content(rng)
        body_hash = hashlib.sha256(content.encode()).hexdigest()
        body = {
            "namespace": ns,
            "title": f"perf-seeded-{i:06d}",
            "content": content,
            "vault_path": f"{cfg.namespace_prefix}/curated/{i:06d}.md",
            "body_hash": body_hash,
            "tags": rng.sample(_TOPICS, k=rng.randint(1, 3)),
        }
        r = post_with_backoff(client, "/curated-knowledge", rng=backoff_rng, json_body=body)
        if r is not None and r.is_success:
            ok += 1
        elif r is not None:
            log.warning("curated %d: %d %s", i, r.status_code, r.text[:160])
        else:
            log.warning("curated %d: skipped (transport error or 429 exhaustion)", i)
        if i and i % 500 == 0:
            log.info("curated: %d / %d (ok=%d)", i, cfg.size, ok)
    return ok


def seed_artifact(client: httpx.Client, cfg: SeedConfig, rng: random.Random) -> int:
    """POST /v1/artifacts — multipart. Smaller volume by default would
    be sensible; artifacts are heavier. Kept at --size for symmetry;
    tune via --planes if you want to skip.

    Idempotency-on-retry: artifact storage is content-addressed by
    SHA256 of the blob; same --seed produces the same bytes so the
    server dedupes on re-run."""
    ns = f"{cfg.namespace_prefix}/artifact"
    ok = 0
    backoff_rng = random.Random(cfg.seed ^ 0xA57)
    for i in range(cfg.size):
        # Multi-section markdown so the chunker has work to do.
        content = (
            f"# perf-seeded-{i:06d}\n\n"
            f"## Section A\n\n{make_content(rng)}\n\n"
            f"## Section B\n\n{make_content(rng)}\n\n"
            f"## Section C\n\n{make_content(rng)}\n"
        ).encode()
        data = {
            "namespace": ns,
            "title": f"perf-seeded-{i:06d}.md",
            "content_type": "text/markdown",
            "source_system": "perf-seed",
            "chunker": "markdown-headings-v1",
        }
        files = {"file": (f"perf-seeded-{i:06d}.md", content, "text/markdown")}
        r = post_with_backoff(client, "/artifacts", rng=backoff_rng, data=data, files=files)
        if r is not None and r.is_success:
            ok += 1
        elif r is not None:
            log.warning("artifact %d: %d %s", i, r.status_code, r.text[:160])
        else:
            log.warning("artifact %d: skipped (transport error or 429 exhaustion)", i)
        if i and i % 250 == 0:
            log.info("artifact: %d / %d (ok=%d)", i, cfg.size, ok)
    return ok


_SEEDERS = {
    "episodic": seed_episodic,
    "curated": seed_curated,
    "artifact": seed_artifact,
    "thought": seed_thought,
}


def main() -> int:
    cfg = parse_args()
    log.info(
        "seeding: size=%d seed=%d planes=%s namespace_prefix=%s target=%s",
        cfg.size,
        cfg.seed,
        cfg.planes,
        cfg.namespace_prefix,
        cfg.base_url,
    )

    started = time.monotonic()
    totals: dict[str, int] = {}

    with httpx.Client(
        base_url=cfg.base_url,
        headers={
            "Authorization": f"Bearer {cfg.token}",
            "User-Agent": "musubi-perf-seed/1",
        },
        timeout=cfg.request_timeout_s,
    ) as client:
        for plane in cfg.planes:
            # Per-plane RNG fork so runs with --planes=episodic,thought
            # produce the same episodic+thought content as a full run.
            rng = random.Random(f"{cfg.seed}:{plane}")
            log.info("=== plane=%s ===", plane)
            totals[plane] = _SEEDERS[plane](client, cfg, rng)

    elapsed = time.monotonic() - started
    log.info("done in %.1fs. totals: %s", elapsed, totals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
