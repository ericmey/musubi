---
title: Contract Tests
section: 07-interfaces
tags: [adapters, contract, interfaces, section/interfaces, status/complete, testing, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[07-interfaces/index]]"
reviewed: false
---
# Contract Tests

The single test suite every adapter runs against a real Musubi instance. If your adapter passes the canonical contract suite, it behaves correctly with respect to the Musubi API — no surprises, no divergent semantics.

**Repo:** `musubi-contract-tests`. Pip-installable as `musubi-contract-tests`. Adapters depend on it in their test extras.

## Why a shared suite

Every adapter — Python SDK, MCP, LiveKit, OpenClaw — goes through the canonical API. They all share the same assumptions: auth works a certain way, errors look a certain way, idempotency keys behave a certain way. If each adapter re-wrote those tests, we'd end up with subtle drift. A shared suite fixes that.

Four properties:

1. **Versioned** — the suite is tagged per Musubi API version. An adapter pinned to `musubi-contract-tests==1.4.*` proves it passes the 1.4 contract.
2. **Black-box** — every test runs via HTTP against a live Musubi Core. No private imports. No peeking at the DB.
3. **Transport-parametrized** — each test runs twice (REST and gRPC) where both are supported.
4. **Seed + teardown** — each test is hermetic; creates its own data in a scoped namespace, cleans up after itself.

## Layout

```
musubi-contract-tests/
  musubi_contract/
    __init__.py
    conftest.py              # pytest fixtures: musubi_url, token, scoped_namespace
    fixtures.py              # seed data builders (memory, artifact, thought)
    auth.py                  # token minting for test scopes
    cases/
      capture/
        test_capture_happy.py
        test_capture_dedup.py
        test_capture_idempotency.py
      retrieve/
        test_retrieve_fast_path.py
        test_retrieve_deep_path.py
        test_retrieve_blended.py
        test_retrieve_filters.py
      thoughts/
        test_thoughts_send_check_read.py
        test_thoughts_history.py
      artifacts/
        test_artifact_upload.py
        test_artifact_download.py
        test_artifact_chunks.py
      lifecycle/
        test_episodic_maturation.py
        test_concept_synthesis_offline.py
        test_promotion_gate.py
      errors/
        test_error_shapes.py
        test_rate_limits.py
      auth/
        test_scope_enforcement.py
    suites/
      canonical.py            # the full v1 suite
      smoke.py                # subset: 30s smoke
      perf.py                 # latency budgets
  pyproject.toml
```

## Fixtures

### `musubi_url`

Points to a running Musubi Core. In CI: a container brought up by `docker compose`. Locally: whatever the developer runs.

```python
@pytest.fixture(scope="session")
def musubi_url() -> str:
    url = os.environ.get("MUSUBI_URL", "http://localhost:8100/v1")
    # Wait until /ops/health is 200.
    for _ in range(30):
        try:
            r = httpx.get(f"{url}/ops/health", timeout=1)
            if r.status_code == 200:
                return url
        except Exception:
            pass
        time.sleep(1)
    pytest.fail("Musubi not reachable")
```

### `scoped_namespace`

Every test gets its own throwaway namespace to keep runs hermetic:

```python
@pytest.fixture
def scoped_namespace(musubi_url, test_token) -> str:
    ns = f"test/{ksuid()}/episodic"
    yield ns
    # Teardown: archive any captured memories in this namespace.
    cleanup_namespace(musubi_url, test_token, ns)
```

Tests use `scoped_namespace` rather than touching production namespaces. Teardown is best-effort — it catches leaks if a test fails.

### `test_token`

Mints a token with scope matching the test's needs:

```python
@pytest.fixture
def test_token(scoped_namespace) -> str:
    return mint_test_token(scopes=[f"{scoped_namespace}:rw"])
```

## Canonical test cases

### Capture

```python
def test_capture_happy(musubi_url, test_token, scoped_namespace):
    r = post(f"{musubi_url}/memories",
             token=test_token,
             json={
                 "namespace": scoped_namespace,
                 "content": "test-content-unique-abc123",
                 "tags": ["contract-test"],
                 "importance": 5,
             })
    assert r.status_code == 201
    body = r.json()
    assert body["object_id"].startswith("k")  # ksuid
    assert body["state"] == "provisional"
    # It should be retrievable.
    got = get(f"{musubi_url}/memories/{body['object_id']}", token=test_token)
    assert got.status_code == 200
    assert got.json()["content"] == "test-content-unique-abc123"
```

```python
def test_capture_dedup_updates_existing(musubi_url, test_token, scoped_namespace):
    content = f"dedup-test-{ksuid()}"
    r1 = post_capture(musubi_url, test_token, scoped_namespace, content, tags=["a"])
    r2 = post_capture(musubi_url, test_token, scoped_namespace, content, tags=["b"])
    # Both should succeed; r2 should return the same object_id as r1 plus dedup info.
    assert r2.json()["object_id"] == r1.json()["object_id"]
    assert r2.json()["dedup"]["action"] == "merged"
    # Tags should now be the union.
    got = get(f"{musubi_url}/memories/{r1.json()['object_id']}", token=test_token)
    assert set(got.json()["tags"]) == {"a", "b"}
```

```python
def test_capture_idempotency_key_returns_same_id(...):
    key = str(uuid.uuid4())
    body = {...}
    r1 = post_with_idempotency_key(key, body)
    r2 = post_with_idempotency_key(key, body)
    assert r1.json()["object_id"] == r2.json()["object_id"]
```

### Retrieve

```python
def test_retrieve_fast_path_returns_relevant_result(...):
    # Seed: capture 5 memories, one clearly relevant to "pizza".
    seed_memories(...)
    r = post(f"{musubi_url}/retrieve",
             token=test_token,
             json={
                 "namespace": scoped_namespace,
                 "query_text": "pizza",
                 "mode": "fast",
                 "limit": 5,
             })
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) >= 1
    assert "pizza" in results[0]["content"].lower()
    assert results[0]["score"] > 0.5
```

```python
def test_retrieve_blended_cross_plane(...):
    # Seed episodic + curated + concept.
    ...
    r = post(f"{musubi_url}/retrieve",
             json={"namespace": "test/_shared/blended", "query_text": "...",
                   "mode": "deep", "planes": ["episodic", "curated", "concept"]})
    planes = {res["plane"] for res in r.json()["results"]}
    assert {"episodic", "curated"}.issubset(planes)
```

```python
def test_retrieve_filters_respected(...):
    # Seed memories with tag=foo and tag=bar.
    ...
    r = post(retrieve, json={..., "filters": {"tags_any": ["foo"]}})
    for res in r.json()["results"]:
        assert "foo" in res["tags"]
```

### Thoughts

```python
def test_thought_send_check_read_roundtrip(musubi_url, test_token):
    presence = f"test-presence-{ksuid()}"
    other = f"test-other-{ksuid()}"
    r = post(f"{musubi_url}/thoughts/send",
             token=test_token,
             json={"from_presence": presence,
                   "to_presence": other,
                   "content": "hello"})
    tid = r.json()["object_id"]
    # other checks inbox.
    check = post(f"{musubi_url}/thoughts/check",
                 token=test_token,
                 json={"my_presence": other})
    assert any(t["object_id"] == tid for t in check.json()["thoughts"])
    # Mark read.
    post(f"{musubi_url}/thoughts/read",
         token=test_token,
         json={"my_presence": other, "ids": [tid]})
    # Re-check: should be empty.
    check2 = post(...)
    assert not any(t["object_id"] == tid for t in check2.json()["thoughts"])
```

```python
def test_thought_self_filtered(...):
    # Sending from presence=A to all should not appear in A's check.
    ...
```

### Artifacts

```python
def test_artifact_upload_download_roundtrip(...):
    content = b"<html>test</html>"
    r = post_multipart(f"{musubi_url}/artifacts", token,
                       data={"namespace": ns, "content_type": "text/html",
                             "title": "test", "source_system": "contract-test"},
                       files={"file": ("page.html", content)})
    oid = r.json()["object_id"]
    got = get(f"{musubi_url}/artifacts/{oid}/blob", token)
    assert got.content == content
```

```python
def test_artifact_chunks_listed_after_upload(...):
    # Upload HTML → chunker produces N chunks → list should return them.
    ...
```

### Lifecycle

Lifecycle tests are tricky because jobs are triggered on their own schedule. The contract suite uses an **operator-only test hook** to trigger a job synchronously:

```python
def test_episodic_maturation_transitions_state(musubi_url, operator_token):
    # Capture → still provisional.
    oid = capture_memory(...)
    assert get_memory(oid)["state"] == "provisional"
    # Trigger maturation.
    post(f"{musubi_url}/ops/run-job",
         token=operator_token,
         json={"job": "episodic_maturation", "target": oid})
    assert get_memory(oid)["state"] == "matured"
```

`/v1/ops/run-job` is gated behind operator scope and disabled by default (`MUSUBI_ALLOW_TEST_HOOKS=true` in test env). Production never exposes it.

### Errors

```python
def test_error_shape_consistent(...):
    # Trigger each error type; verify each returns the structured shape.
    cases = [
        ("bad_request", {"namespace": "", ...}, 400, "BAD_REQUEST"),
        ("forbidden", {"namespace": "other/ns/..."}, 403, "FORBIDDEN"),
        ("not_found", "/memories/nonexistent", 404, "NOT_FOUND"),
    ]
    for _, request, expected_status, expected_code in cases:
        r = do(request)
        assert r.status_code == expected_status
        assert r.json()["error"]["code"] == expected_code
        assert "detail" in r.json()["error"]
        assert "hint" in r.json()["error"]
```

### Auth

```python
def test_out_of_scope_namespace_returns_403(...):
    # Token scope: eric/test-a/episodic:rw
    # Capture to eric/test-b/episodic → 403.
    r = post_capture(..., namespace="eric/test-b/episodic")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"
```

### Rate limits

```python
def test_capture_rate_limit_kicks_in(...):
    # 110 captures in a minute → at least one 429.
    responses = [post_capture(...) for _ in range(110)]
    statuses = [r.status_code for r in responses]
    assert 429 in statuses
    # Honored Retry-After.
    rl = next(r for r in responses if r.status_code == 429)
    assert int(rl.headers["Retry-After"]) >= 1
```

## Test suites

Three named suites compose the cases:

### `canonical`

The full suite — ~60 tests, ~3 minutes. Every adapter runs this as part of its integration tests.

### `smoke`

30-second sanity check — ~10 tests: capture, recall, thought roundtrip, artifact upload, error shape. Used for deploy verification.

### `perf`

Latency budget checks. Uses a warmed Musubi Core and asserts p50/p95 against budgets from [[07-interfaces/canonical-api]]:

- Capture p95 < 300ms.
- Fast retrieve p95 < 400ms.
- Deep retrieve p95 < 5s.
- Artifact upload (1MB) p95 < 500ms.

Not run in unit CI (too noisy); runs nightly on the dedicated Ubuntu box.

## Running

```bash
# From an adapter repo, after installing `musubi-contract-tests`:
pytest --contract=canonical --musubi-url=http://localhost:8100/v1

# Operator-only tests (lifecycle hooks):
pytest --contract=canonical --operator-token=...

# Skip long tests:
pytest --contract=smoke
```

The `--contract` plugin is part of `musubi-contract-tests`; it selects tests by suite name.

## Adapter integration

Each adapter's CI runs the canonical suite against its adapter + a live Musubi Core. For example:

```yaml
# musubi-mcp-adapter/.github/workflows/integration.yml
- name: Start Musubi
  run: docker compose up -d musubi
- name: Run contract suite via adapter
  run: pytest --contract=canonical --musubi-url=<adapter-transport>
```

The adapter sits between the test client and Musubi. If the adapter drops a field, reshapes a response, or handles an error differently — the contract suite catches it.

## Versioning

The suite ships with a version tied to the Musubi API major version:

```
musubi-contract-tests==1.0.*   # v1 API
musubi-contract-tests==2.0.*   # v2 API (if/when)
```

Within a major version, the suite can add **new tests** (adapters should pass) but never remove or weaken them. Adapters set a floor pin:

```toml
[project.optional-dependencies]
test = ["musubi-contract-tests>=1.3,<2"]
```

CI running a newer `1.x` releases against an older adapter surfaces gaps — a useful signal, not a crash.

## What the suite does NOT cover

- **Adapter-specific behavior** (e.g., MCP tool schemas, LiveKit Fast Talker cache). Those are in the adapter's own test suite.
- **Load / chaos** tests. Separate `musubi-perf` repo uses locust/vegeta.
- **LLM quality** (concept synthesis output quality). Covered by [[05-retrieval/evals]] offline eval sets.

## Test contract (meta)

**Module under test:** `musubi-contract-tests/*`

Self-tests:

1. `test_every_test_declares_scoped_namespace`
2. `test_teardown_archives_created_data`
3. `test_suite_runs_clean_against_reference_musubi`
4. `test_smoke_suite_completes_under_30s_on_reference_hw`
5. `test_operator_hooks_gated_behind_env_flag`
6. `test_transport_parametrization_covers_rest_and_grpc` (where grpc enabled)

Cross-cutting:

7. `test_all_error_shapes_are_consistent_across_endpoints`
8. `test_no_test_leaks_data_between_runs`
9. `test_suite_version_tagged_against_api_major`
