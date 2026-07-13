"""
RET-003 wire contract: 18 acceptance tests (15 strict reds + 3 guards).

This is the tests-first slice (zero src in the first commit). All 18
tests will FAIL against the current main because the implementation has
not landed yet; that is expected. The implementation lands in a follow-up
slice after the test contract is accepted.

The tests are organized per the locked spec at:
  projects/active/hermes-musubi-provider/specs/spec-ret003-ranked-recent-wire-contract.md

See docs/Musubi/_slices/slice-api-v1-ret003-wire.md for the slice contract.
"""



# The tests below are the acceptance contract for RET-003. Each test name
# matches the spec exactly. The implementation will satisfy them in a
# follow-up commit; this commit establishes the contract only.

# =====================================================================
# SECTION 6.1 — Ranked-mode wire shape (7 strict reds + 1 guard)
# =====================================================================


def test_retrieve_ranked_top_level_state_present_required_nullable() -> None:
    """`state` key is present on every row, may be `null` for legacy."""
    # Implementation contract: row has `state` key (nullable).
    assert False, "RED — implementation must add top-level `state` to the row schema (nullable for legacy)"


def test_retrieve_ranked_state_is_source_backed_not_fabricated() -> None:
    """Valid + invalid source values. 500 on bad enum, not 422."""
    # Implementation contract: present-valid → exact source; present-invalid → 500.
    assert False, "RED — state must be source-backed; invalid values must fail loud (500), not be coerced to valid"


def test_retrieve_ranked_top_level_importance_present_required_nullable() -> None:
    """`importance` key is present on every row, may be `null` for legacy."""
    # Implementation contract: row has `importance` key (nullable, 1..10 when present).
    assert False, "RED — implementation must add top-level `importance` to the row schema (nullable for legacy)"


def test_retrieve_ranked_importance_is_source_backed_not_fabricated() -> None:
    """Valid + invalid source values. 500 on out-of-range, not 422."""
    # Implementation contract: present-valid (1..10) → exact source; present-invalid → 500.
    assert False, "RED — importance must be source-backed; out-of-range values must fail loud (500), not be coerced"


def test_retrieve_ranked_score_kind_is_ranked_combined() -> None:
    """`score_kind` is the literal string 'ranked_combined' for every ranked row."""
    # Implementation contract: `score_kind` field on the row, value is "ranked_combined".
    assert False, "RED — implementation must add `score_kind: Literal['ranked_combined']` to the row schema"


def test_retrieve_ranked_extra_score_components_has_five_keys() -> None:
    """5 keys in extra.score_components (compat path); brief=true preserves state/importance."""
    # Implementation contract: 5 typed fields, all required, Field(ge=0,le=1), extra='forbid'.
    assert False, "RED — implementation must add typed `RankedScoreComponents` with 5 required fields; brief=true must preserve state/importance"


def test_retrieve_ranked_score_is_combined_from_components() -> None:
    """`score` equals weights.combine(**test-local public-to-internal mapping) (float tolerance)."""
    # Implementation contract: the test maps public `reinforcement` to internal `reinforce` locally.
    assert False, "RED — implementation must preserve score = weights.combine(...) across the public/internal boundary; test-local mapping helper"


def test_retrieve_ranked_reinforcement_uses_full_word() -> None:
    """`extra.score_components.reinforcement` exists; no `reinforce` key (guard)."""
    # Implementation contract: public name is `reinforcement` (full word) on the wire.
    # This is a REGRESSION GUARD — already passes current wire in the public shape.
    # Implementation is the source of the guard; if the production code reverts to
    # singular `reinforce`, this test will fail. Implementation may add this guard
    # alongside the wire changes.
    assert False, "GUARD — production must keep public key `reinforcement` (full word); test will fail if implementation reverts"


# =====================================================================
# SECTION 6.2 — Recent-mode wire shape (5 strict reds)
# =====================================================================


def test_retrieve_recent_score_kind_is_created_epoch() -> None:
    """`score_kind` is the literal string 'created_epoch' for every recent row."""
    # Implementation contract: recent branch sets `score_kind: Literal['created_epoch']`.
    assert False, "RED — implementation must set `score_kind: Literal['created_epoch']` in the recent branch"


def test_retrieve_recent_extra_score_components_is_empty_dict_typed() -> None:
    """`extra.score_components` is exactly `{}` typed RecentScoreComponents (never null)."""
    # Implementation contract: recent branch produces an EXACT empty dict; typed
    # RecentScoreComponents with no fields and `model_config = ConfigDict(extra='forbid')`.
    assert False, "RED — implementation must produce exact {} (typed RecentScoreComponents); never null; non-empty input fails (500)"


def test_retrieve_recent_top_level_state_present() -> None:
    """Same as #1 but for recent mode (nullable for legacy)."""
    # Implementation contract: recent row has `state` key (nullable).
    assert False, "RED — implementation must add top-level `state` (nullable) to RecentResultRow"


def test_retrieve_recent_top_level_importance_present() -> None:
    """Same as #3 but for recent mode (nullable for legacy)."""
    # Implementation contract: recent row has `importance` key (nullable).
    assert False, "RED — implementation must add top-level `importance` (nullable) to RecentResultRow"


def test_retrieve_recent_provenance_score_is_nullable_not_fabricated() -> None:
    """`provenance_score` is None for missing state or absent `(plane, state)`; otherwise exact value."""
    # Implementation contract: `_provenance_score_for(plane, state) -> float | None` returns None for missing state
    # or absent (plane, state); otherwise the value from `_PROVENANCE`. Does NOT call `scoring._provenance`.
    # 3 cases: (a) exact known-table value, (b) missing-state null, (c) absent-pair null with VALID state.
    assert False, "RED — implementation must use explicit `_provenance_score_for` that returns None for missing state or absent (plane, state); NOT scoring._provenance (which floors to 0.1)"


# =====================================================================
# SECTION 6.3 — Source-truth vs internal-default (1 strict red)
# =====================================================================


def test_wire_importance_audits_internal_default() -> None:
    """Raw `importance` is null for missing source; `score_components.importance` is 0.5 from internal Hit default."""
    # Implementation contract: distinction between wire (raw, source-backed, nullable) and
    # score_components (normalized 0..1, computed from internal Hit default if missing). Both
    # fields are exposed; their values differ for legacy rows.
    assert False, "RED — implementation must expose raw `importance` (nullable) AND `score_components.importance` (0.5 from internal default); the difference is auditable"


# =====================================================================
# SECTION 6.4 — Runtime OpenAPI schema (2 strict reds)
# =====================================================================


def test_runtime_openapi_ranked_response_schema_required_with_five_components() -> None:
    """GET /v1/openapi.json → RankedRetrieveResponse required has [mode, results, limit]; RankedResultRow required has 7 fields; RankedScoreComponents has 5 properties."""
    # Implementation contract: runtime FastAPI Pydantic models produce the openapi.json with
    # the required arrays on the row schemas; RankedScoreComponents has exactly 5 properties.
    assert False, "RED — implementation must add the 5 typed fields to RankedScoreComponents; the runtime openapi.json must reflect them"


def test_runtime_openapi_recent_response_schema_required_with_empty_components() -> None:
    """GET /v1/openapi.json → RecentRetrieveResponse required has [mode, results, limit]; RecentResultRow required has 7 fields; RecentScoreComponents is exact {} (additionalProperties:false)."""
    # Implementation contract: runtime FastAPI Pydantic models produce the openapi.json with
    # RecentScoreComponents as the exact empty object (additionalProperties: false).
    assert False, "RED — implementation must make RecentScoreComponents exact {} (additionalProperties:false) in the runtime openapi.json"


# =====================================================================
# SECTION 6.5 — Regression guards (2 — but a third is reclassified #8 from §6.1)
# =====================================================================


def test_streaming_endpoint_excluded_from_this_contract_unchanged() -> None:
    """`/v1/retrieve/stream` is RET-010 (out of scope for RET-003). Unchanged behavior."""
    # Implementation contract: this slice does NOT touch `src/musubi/api/routers/writes_retrieve_stream.py`.
    assert False, "GUARD — implementation must NOT change the streaming endpoint; unchanged behavior is required"


def test_extra_score_components_path_preserved_for_all_modes() -> None:
    """`extra.score_components` is present at the same path for both ranked and recent."""
    # Implementation contract: this path is preserved (v1 compat); ranked expands 3→5; recent is {}.
    assert False, "GUARD — implementation must keep `extra.score_components` at the same path; do NOT migrate to top-level in v1"
