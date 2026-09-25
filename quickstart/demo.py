"""Musubi quickstart demo: one agent learns, another agent recalls.

Run it against the quickstart stack:

    docker compose -f quickstart/docker-compose.yml run --rm demo

What it shows, and checks (exits non-zero if any check fails):

1. A "scribe" agent captures a few memories into its own namespace.
2. The memories are matured. In a real deployment the hourly lifecycle
   sweep does this; here an operator token does it directly so the demo
   does not wait an hour.
3. A separate "helper" agent, holding a READ-ONLY token for the scribe's
   namespace, asks a question in different words than any memory uses
   and gets the right memory back (dense + sparse hybrid, then rerank).
4. The helper tries to write into the scribe's namespace and is refused.

Tokens are minted locally with the PUBLIC quickstart signing key from
.env.quickstart. That key exists only for this demo.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import jwt

API = os.environ.get("MUSUBI_URL", "http://localhost:8100/v1")
KEY = os.environ["JWT_SIGNING_KEY"]
NS = "demo/scribe/episodic"
RUN = uuid.uuid4().hex[:6]

MEMORIES = [
    "Eric drinks a flat white with oat milk every morning before stand-up.",
    "The staging database is backed up nightly at 02:00 to the NAS.",
    "Tama prefers code reviews that name the failing case, not a style nit.",
    "The render server needs a restart after every driver update.",
]
QUESTION = "Which coffee order does the boss like at the start of the day?"
EXPECTED = 0  # index into MEMORIES
STOPWORDS = {"a", "an", "the", "of", "at", "to", "does", "which", "with", "every", "before", "like"}


def content_words(text: str) -> set[str]:
    return {w.strip(".,?").lower() for w in text.split()} - STOPWORDS


def token(sub: str, scopes: list[str]) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "https://auth.quickstart.local",
            "sub": sub,
            "aud": "musubi",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=30)).timestamp()),
            "jti": f"{sub}-{RUN}",
            "scope": " ".join(scopes),
            "presence": sub,
        },
        KEY,
        algorithm="HS256",
    )


def client(tok: str) -> httpx.Client:
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {tok}"}, timeout=30.0)


def fail(msg: str) -> None:
    print(f"\nFAIL: {msg}")
    sys.exit(1)


def main() -> None:
    scribe = client(token("demo/scribe", [f"{NS}:rw"]))
    operator = client(token("demo/operator", ["operator", f"{NS}:rw"]))
    helper = client(token("demo/helper", [f"{NS}:r"]))

    print(f"1. scribe captures {len(MEMORIES)} memories into {NS}")
    ids = []
    for text in MEMORIES:
        r = scribe.post("/episodic", json={"namespace": NS, "content": text, "importance": 6})
        if r.status_code >= 300:
            fail(f"capture returned {r.status_code}: {r.text[:300]}")
        ids.append(r.json()["object_id"])
        print(f"   + {text}")

    print("2. operator matures them (stand-in for the hourly lifecycle sweep)")
    for oid in ids:
        r = operator.post(
            "/lifecycle/transition",
            json={"object_id": oid, "to_state": "matured", "actor": "quickstart", "reason": "demo"},
        )
        if r.status_code >= 300:
            fail(f"transition returned {r.status_code}: {r.text[:300]}")

    shared = content_words(QUESTION) & content_words(MEMORIES[EXPECTED])
    if shared:
        fail(f"demo question shares words with the target memory: {sorted(shared)}")
    print(f'3. helper (read-only token) asks: "{QUESTION}"')
    rows: list[dict] = []
    for _ in range(15):
        r = helper.post(
            "/retrieve", json={"namespace": NS, "query_text": QUESTION, "mode": "fast", "limit": 3}
        )
        if r.status_code >= 300:
            fail(f"retrieve returned {r.status_code}: {r.text[:300]}")
        rows = r.json().get("results", [])
        if rows:
            break
        time.sleep(1.0)
    if not rows:
        fail("retrieve returned no results")
    for rank, row in enumerate(rows, 1):
        text = row.get("content") or row.get("text") or row.get("object_id")
        score = row.get("score")
        shown = f"{score:.3f}" if isinstance(score, (int, float)) else "n/a"
        print(f"   #{rank}  score={shown}  {text}")
    if rows[0].get("object_id") != ids[EXPECTED]:
        fail("top result is not the morning-coffee memory")
    print("   top result is the right memory; the question shares no content words with it")

    print("4. helper tries to write into the scribe's namespace")
    r = helper.post(
        "/episodic", json={"namespace": NS, "content": "helper was here", "importance": 1}
    )
    if r.status_code != 403:
        fail(f"expected 403 for a read-only token, got {r.status_code}")
    print("   refused with 403: read scope cannot write")

    print("\nOK: one agent learned it, another recalled it, and scopes held.")


if __name__ == "__main__":
    main()
