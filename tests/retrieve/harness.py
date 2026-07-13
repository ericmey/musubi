"""Neutral observation harness for Musubi retrieval.

Approved by Yua (router, 2026-07-12) with a hard boundary:

    "Build neutral reusable infrastructure + red tests for reproduced D1-D3 only.
     Do NOT encode golden corpus, MRR threshold, cross-plane comparability, abstention
     floor, lifecycle policy, or namespace semantics until I settle those contracts.
     Keep raw observations separate from verdict assertions."

So this module **observes and reports**. It asserts nothing about what SHOULD be true.
The contracts are hers; the measurements are ours.

THE ONE RULE THIS FILE EXISTS TO ENFORCE
----------------------------------------
**A probe must never perturb what it measures, and must never report its own cutoff as
a finding.**

Every measurement error in the 2026-07-12 audit was one of these:

  * ``grep | head -3``                → "1Password is broken"      (it wasn't)
  * ``--limit 50``                    → "265 memories fleet-wide"  (it's 656)
  * the five newest rows              → "everything is provisional" (sampling artifact)
  * the last 40 log lines             → "synthesis isn't running"   (it runs at 3am)
  * a default ``state_filter``, twice → "the concept plane is empty" (it isn't)
  * ``.get("importance", 0)``         → "25% of ranking is dead"    (the key is absent)

Each time the instrument was cut short, and the cut was read as the truth.

Concretely, here:

  * ``access_count`` is read from the **Qdrant payload**, never through ``GET /episodic``
    — because **GET itself increments ``access_count``**. Measuring with a GET means
    measuring your own probe.
  * counts are read from the **store**, never from a paged API whose ``limit`` you chose.
  * absent keys are reported as ``None``, never defaulted to ``0``.
"""

from __future__ import annotations

import base64
import json
import random
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

VALID_MODES = ("fast", "blended", "deep", "recent")
ALL_STATES = ["provisional", "matured", "promoted", "demoted", "archived", "superseded"]

# Deliberately NOT a contract. This is the set a caller must pass today to see a memory
# it just wrote. Whether that should be the server default is Yua's call, not ours.
FRESH_STATES = ["provisional", "matured", "promoted"]

_SENTINEL = object()


def _env(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )


@dataclass(frozen=True)
class Observation:
    """A raw measurement. No verdict. No defaults.

    ``value is None`` means **the key was absent**, not "the value was zero." Those are
    different facts and conflating them is exactly how a missing response field became
    "25% of the ranking weight is dead."
    """

    name: str
    value: Any
    source: str  # "qdrant" | "api" | "log" — WHERE it was read matters
    note: str = ""

    def __str__(self) -> str:  # pragma: no cover - human output
        v = "<absent>" if self.value is None else self.value
        return f"{self.name}={v}  [{self.source}]{'  ' + self.note if self.note else ''}"


@dataclass
class Musubi:
    """Talks to the API. Never used to measure anything the API mutates."""

    env_file: Path
    _url: str = field(init=False)
    _tok: str = field(init=False)

    def __post_init__(self) -> None:
        e = _env(self.env_file)
        self._url = e["MUSUBI_API_URL"].rstrip("/")
        self._tok = e["MUSUBI_TOKEN"]

    def _req(self, method: str, path: str, body: Optional[dict] = None,
             query: Optional[dict] = None) -> dict:
        url = f"{self._url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode() if body else None, method=method)
        req.add_header("Authorization", f"Bearer {self._tok}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")

    def write(self, namespace: str, content: str, *, importance: int = 5,
              tags: Optional[list[str]] = None) -> str:
        payload = self._req("POST", "/episodic", {
            "namespace": namespace,
            "content": content,
            "tags": tags or ["kind:episode", "staleness:episodic"],
            "importance": importance,
        })
        return payload["object_id"]

    def recall(self, namespace: str, query: str, *, mode: str = "blended",
               limit: int = 5, state_filter: Optional[list[str]] = _SENTINEL  # type: ignore[assignment]
               ) -> list[dict]:
        """Raw ranked recall.

        ``state_filter=None`` means **send no filter** — i.e. exercise the SERVER
        DEFAULT, which is the thing under test in D1. It does not mean "no filtering."
        Passing the sentinel (the default) sends FRESH_STATES.
        """
        body: dict[str, Any] = {
            "namespace": namespace, "query_text": query, "mode": mode, "limit": limit}
        if state_filter is not _SENTINEL and state_filter is not None:
            body["state_filter"] = state_filter
        elif state_filter is _SENTINEL:
            body["state_filter"] = FRESH_STATES
        d = self._req("POST", "/retrieve", body)
        return d.get("results") or d.get("data") or []

    # NOTE: there is deliberately NO `get()` helper here.
    #
    # GET /episodic/{id} INCREMENTS access_count. A harness that measures access_count
    # through a GET is measuring itself. If you need the object, read the store.


@dataclass
class Store:
    """Reads the Qdrant payload directly. **The only trustworthy measurement surface.**

    Not because the API lies — because the API *mutates*. `GET` bumps `access_count`.
    Any probe that reads through it is a participant, not an observer.
    """

    host: str = "musubi"
    collection: str = "musubi_episodic"

    def _exec(self, script: str) -> str:
        """Ship the probe as base64.

        A newline in a `python3 -c` argument does not survive ssh + shell quoting — it
        arrives as a literal backslash-n and the probe dies. Base64 has no quoting
        surface at all. A measurement tool that can be broken by a quote is a
        measurement tool that will eventually lie to you quietly instead of loudly.
        """
        b64 = base64.b64encode(script.encode()).decode()
        remote = (
            "sudo -n docker exec musubi-core-1 python3 -c "
            f"\"import base64;exec(base64.b64decode('{b64}').decode())\""
        )
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", self.host, remote],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            raise RuntimeError(f"store read failed: {out.stderr.strip()[:200]}")
        return out.stdout.strip()

    def payload(self, object_id: str) -> Optional[dict]:
        """The raw stored payload, with NO side effects. Returns None if absent."""
        script = (
            'import os,json,urllib.request\n'
            'k=os.environ["QDRANT_API_KEY"]\n'
            'r=urllib.request.Request("http://qdrant:6333/collections/%s/points/scroll",'
            'data=json.dumps({"limit":1,"with_payload":True,"filter":{"must":['
            '{"key":"object_id","match":{"value":"%s"}}]}}).encode(),method="POST")\n'
            'r.add_header("api-key",k)\n'
            'r.add_header("Content-Type","application/json")\n'
            'p=json.load(urllib.request.urlopen(r,timeout=15))["result"]["points"]\n'
            'print(json.dumps(p[0]["payload"] if p else None))'
        ) % (self.collection, object_id)
        return json.loads(self._exec(script))

    def observe(self, object_id: str, key: str) -> Observation:
        """One field, from the store. **Absent is reported as absent, never as 0.**"""
        pl = self.payload(object_id)
        if pl is None:
            return Observation(key, None, "qdrant", note="object not found in store")
        return Observation(key, pl.get(key), "qdrant",
                           note="" if key in pl else "KEY ABSENT (not zero)")

    def count(self, collection: Optional[str] = None) -> Observation:
        """The TRUE point count. Not a paged API's ``limit`` handed back to you."""
        col = collection or self.collection
        script = (
            'import os,json,urllib.request\n'
            'k=os.environ["QDRANT_API_KEY"]\n'
            'r=urllib.request.Request("http://qdrant:6333/collections/%s")\n'
            'r.add_header("api-key",k)\n'
            'print(json.load(urllib.request.urlopen(r,timeout=15))["result"]["points_count"])'
        ) % col
        return Observation(f"{col}.points_count", int(self._exec(script)), "qdrant")


@dataclass
class Fixture:
    """A known memory, seeded and confirmed **without touching it through the API**."""

    musubi: Musubi
    store: Store
    namespace: str

    # A seed is only a seed if the STORE agrees it is a new memory.
    #
    # Yua killed two attempts at this, and was right both times:
    #
    #   1. Every fixture shared one phrasing, so a recall aimed at one MATCHED AND MARKED
    #      the others. Cross-contamination; every delta was noise.
    #   2. Fixing that with 8 orthogonal stems only made collision *less likely* — the
    #      list CYCLES, and Musubi SEMANTICALLY DEDUPES on capture, so a repeated stem
    #      returns an EXISTING object_id carrying a prior access_count.
    #
    #      "Do not expand stem list as a probabilistic fix; instrument must verify
    #       postcondition."
    #
    # So: random content per seed, and then PROVE newness against the raw store. If the
    # write deduped into an existing memory, the FIXTURE FAILS LOUDLY. A probe that
    # cannot prove its own setup cannot prove anything else.
    _WORDS = (
        "quorate", "lantern", "marmalade", "ferret", "tidal", "vellum", "orchard",
        "granite", "lullaby", "turnstile", "cutlery", "obsidian", "domino", "kelp",
        "registrar", "comet", "chalk", "hummingbird", "cellar", "brass", "semaphore",
        "cobalt", "aqueduct", "filigree", "zephyr", "quarry", "meridian", "thistle",
        "cantilever", "plumbago", "vestibule", "gantry", "isinglass", "wicket",
    )

    def _fresh_content(self, marker: str) -> str:
        body = " ".join(random.sample(self._WORDS, 9))
        return f"{marker}: {body}. Fixture content is random per seed and never reused."

    def seed(self, *, importance: int = 5, settle_s: float = 1.5) -> tuple[str, str]:
        """Write a memory and PROVE it is new. Returns (object_id, marker).

        Confirmed via the STORE, never via GET — a GET increments `access_count`, so
        verifying a fixture through the API means every access measurement downstream is
        reading the fixture's own setup.
        """
        marker = f"hx{uuid.uuid4().hex[:10]}"
        oid = self.musubi.write(
            self.namespace, self._fresh_content(marker), importance=importance)

        deadline = time.time() + 25
        pl: Optional[dict] = None
        while time.time() < deadline:
            pl = self.store.payload(oid)
            if pl is not None:
                break
            time.sleep(settle_s)
        if pl is None:
            raise RuntimeError(f"seeded object {oid} never appeared in the store")

        # ── THE SEED INVARIANT (Yua) ─────────────────────────────────────────────
        problems = []
        ver = pl.get("version")
        if ver not in (1, None):
            problems.append(f"version={ver} (expected 1 — this row is NOT new)")
        if (pl.get("access_count") or 0) != 0:
            problems.append(
                f"access_count={pl.get('access_count')} (expected 0 — already accessed)")
        if (pl.get("reinforcement_count") or 0) != 0:
            problems.append(
                f"reinforcement_count={pl.get('reinforcement_count')} (expected 0 — "
                f"CAPTURE DEDUPED INTO AN EXISTING MEMORY)")
        if problems:
            raise RuntimeError(
                f"SEED INVARIANT VIOLATED for {oid}: " + "; ".join(problems) +
                ". The write returned an object that is not a new memory — most likely "
                "semantic dedup reinforced an existing row. Any measurement taken against "
                "this fixture would be measuring somebody else's history."
            )
        return oid, marker

    def seed_cohort(self, n: int, *, importance: int = 5) -> tuple[list[str], str]:
        """n memories that share ONE retrievable anchor token but are otherwise distinct.

        The over-marking question needs several fixtures that a single query can surface,
        so the caller's `limit` drops some of them. But fixtures must ALSO be distinct
        enough that capture does not semantically dedupe them into one row — which is
        exactly the trap Yua caught: "'seeded=5' input calls is not five memories."

        So: a shared nonsense anchor (retrievable) + a distinct random body (not
        dedupable). The seed invariant then PROVES each one is new — if capture collapses
        any of them, this raises rather than silently measuring one row five times.
        """
        anchor = f"zt{uuid.uuid4().hex[:8]}"
        oids = []
        for _ in range(n):
            marker = f"hx{uuid.uuid4().hex[:10]}"
            body = " ".join(random.sample(self._WORDS, 9))
            oid = self.musubi.write(
                self.namespace, f"{anchor} {marker}: {body}.", importance=importance)
            oids.append(oid)
        time.sleep(3.0)
        for oid in oids:
            pl = self.store.payload(oid) or {}
            if (pl.get("access_count") or 0) or (pl.get("reinforcement_count") or 0):
                raise RuntimeError(
                    f"SEED INVARIANT VIOLATED for {oid}: access={pl.get('access_count')} "
                    f"reinforcement={pl.get('reinforcement_count')} — capture deduped this "
                    f"cohort; these are not {n} distinct memories.")
        if len(set(oids)) != n:
            raise RuntimeError(f"requested {n}, capture returned {len(set(oids))} distinct ids")
        return oids, anchor

    def seed_many(self, n: int, *, importance: int = 5) -> list[str]:
        """n DISTINCT memories, each proven new. Raises if capture deduped any.

        Yua: "'seeded=5' input calls is not five memories." Exactly — and that is where
        my over-marking claim came from. The count is now a POSTCONDITION, not an intent.
        """
        oids = [self.seed(importance=importance)[0] for _ in range(n)]
        if len(set(oids)) != n:
            raise RuntimeError(
                f"requested {n} fixtures; capture returned {len(set(oids))} distinct "
                f"object_ids — dedup collapsed them. These are not {n} memories.")
        return oids
