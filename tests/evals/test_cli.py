import json
import sys
from pathlib import Path
from typing import Any

import pytest

from musubi.evals import cli


class DefectStillPresent(Exception):
    pass


def _write_fixture(
    data_dir: Path, payload: dict[str, Any], filename: str = "smoke_fixture.json"
) -> None:
    data_dir.mkdir(exist_ok=True)
    fixture_path = data_dir / filename
    with open(fixture_path, "w") as f:
        json.dump(payload, f)

    import hashlib

    with open(fixture_path, "rb") as bf:
        checksum = hashlib.sha256(bf.read()).hexdigest()

    manifest = {"name": "smoke_fixture", "files": {filename: checksum}}
    with open(data_dir / "manifest.json", "w") as f:
        json.dump(manifest, f)

    baseline = {"ndcg@10": 0.0}
    with open(data_dir / "baseline.json", "w") as f:
        json.dump(baseline, f)


def _assert_verbatim_projection(
    runner_calls: list[dict[str, Any]], expected_fixture: dict[str, Any]
) -> None:
    assert len(runner_calls) == 1, "CLI must invoke run_smoke_gate exactly once"
    call = runner_calls[0]
    passed_corpus = call["corpus"]
    passed_query_embedding = call.get("query_embedding")

    assert passed_query_embedding is not None, "CLI must pass query_embedding to the runner"
    assert passed_query_embedding == expected_fixture["query_embedding"], (
        "CLI must pass the exact query_embedding"
    )

    assert len(passed_corpus) == len(expected_fixture["corpus"]), "Corpus length mismatch"
    for i, doc in enumerate(passed_corpus):
        expected_doc = expected_fixture["corpus"][i]
        assert "embedding" in doc, "CLI must not strip document embeddings"
        assert doc["id"] == expected_doc["id"]
        assert doc["text"] == expected_doc["text"]
        assert doc["relevance"] == expected_doc["relevance"]
        assert doc["embedding"] == expected_doc["embedding"]


def _run_cli_and_catch_legacy_defect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], data_dir: Path
) -> list[dict[str, Any]]:
    monkeypatch.setattr(sys, "argv", ["musubi-evals", "smoke", "--data-dir", str(data_dir)])
    runner_calls: list[dict[str, Any]] = []

    def mock_run_smoke_gate(
        corpus: list[dict[str, Any]], query_embedding: list[float] | None = None
    ) -> Any:
        runner_calls.append({"corpus": corpus, "query_embedding": query_embedding})
        return type("MockResult", (), {"metrics": {"ndcg@10": 1.0}})()

    monkeypatch.setattr(cli, "run_smoke_gate", mock_run_smoke_gate)

    try:
        cli.main()
    except SystemExit as exc:
        if exc.code != 0:
            out, err = capsys.readouterr()
            if "Missing corpus or manifest." in out:
                raise DefectStillPresent(
                    "Legacy CLI exits on missing corpus.yaml before invoking the runner"
                )
            if "Schema validation failed" in out:
                raise DefectStillPresent(
                    "Legacy CLI fails schema validation on valid typed extension"
                )
            raise AssertionError(f"CLI exited with {exc.code} for an unrelated reason: {out} {err}")
    except Exception as exc:
        raise AssertionError(f"CLI raised an unrelated exception: {exc}")

    return runner_calls


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: CLI exits on missing corpus.yaml"
)
def test_eval_cli_seam_fixed_embeddings_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    fixture = {
        "query_embedding": [0.1, 0.2, 0.3],
        "corpus": [
            {"id": "doc1", "text": "alpha", "relevance": 3, "embedding": [0.1, 0.2, 0.3]},
            {"id": "doc2", "text": "beta", "relevance": 0, "embedding": [0.9, -0.1, 0.0]},
        ],
    }
    _write_fixture(data_dir, fixture)
    runner_calls = _run_cli_and_catch_legacy_defect(monkeypatch, capsys, data_dir)
    # If the code was fixed to not exit, it might strip embeddings
    # which would cause _assert_verbatim_projection to raise AssertionError, failing the strict xfail!
    # Wait, if we are at an intermediate state where it invokes the runner but strips embeddings, we still want it to fail the test.
    # The prompt says: "an unrelated JSON/IO exception is NOT swallowed as the expected defect"
    _assert_verbatim_projection(runner_calls, fixture)


# --- Red Proofs / Discriminators for the Projection ---


def test_discrimination_correct_projection() -> None:
    fixture = {
        "query_embedding": [0.1],
        "corpus": [{"id": "1", "text": "A", "relevance": 1, "embedding": [0.1]}],
    }
    calls = [{"corpus": fixture["corpus"], "query_embedding": fixture["query_embedding"]}]
    _assert_verbatim_projection(calls, fixture)  # Should pass


def test_discrimination_missing_query_embedding() -> None:
    fixture = {
        "query_embedding": [0.1],
        "corpus": [{"id": "1", "text": "A", "relevance": 1, "embedding": [0.1]}],
    }
    calls = [{"corpus": fixture["corpus"]}]  # Missing query_embedding
    with pytest.raises(AssertionError, match="CLI must pass query_embedding to the runner"):
        _assert_verbatim_projection(calls, fixture)


def test_discrimination_stripped_doc_embedding() -> None:
    fixture = {
        "query_embedding": [0.1],
        "corpus": [{"id": "1", "text": "A", "relevance": 1, "embedding": [0.1]}],
    }
    stripped_corpus = [{"id": "1", "text": "A", "relevance": 1}]
    calls = [{"corpus": stripped_corpus, "query_embedding": [0.1]}]
    with pytest.raises(AssertionError, match="CLI must not strip document embeddings"):
        _assert_verbatim_projection(calls, fixture)


def test_discrimination_bypassed_runner() -> None:
    fixture = {
        "query_embedding": [0.1],
        "corpus": [{"id": "1", "text": "A", "relevance": 1, "embedding": [0.1]}],
    }
    calls: list[dict[str, Any]] = []  # Bypassed
    with pytest.raises(AssertionError, match="CLI must invoke run_smoke_gate exactly once"):
        _assert_verbatim_projection(calls, fixture)


def test_discrimination_unrelated_exception_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Trigger an unrelated error like bad JSON parsing by corrupting the manifest file
    data_dir = tmp_path / "data"
    _write_fixture(data_dir, {"dummy": "data"})

    # Touch corpus.yaml so it bypasses the existence check
    (data_dir / "corpus.yaml").touch()

    with open(data_dir / "manifest.json", "w") as f:
        f.write("{bad json")

    with pytest.raises(AssertionError, match="unrelated"):
        _run_cli_and_catch_legacy_defect(monkeypatch, capsys, data_dir)


# --- Schema Validation Invariants ---


# Define a mock schema validation logic to prove shape validation rules
# Since we are tests/docs only, we write a red contract for the schema parser.
def _parse_smoke_fixture(payload: dict[str, Any]) -> None:
    # The actual implementation will use Pydantic. This helper represents the assertion that the schema validator enforces these rules.
    # To prove the contract, we can just assert that Pydantic/CLI *would* raise on these.
    # Actually, we should test the actual `GoldenQuery` or the new schema if it existed, but we are doing tests-first.
    # We can write a RED test that tries to parse using the FUTURE parser, but since we don't have the future parser yet,
    # we can define a red test that imports the target schema and validates it.
    pass


@pytest.mark.xfail(
    strict=True,
    raises=DefectStillPresent,
    reason="RET-004: SmokeFixture schema not yet implemented",
)
def test_schema_validation_invariants_red() -> None:
    _get_smoke_fixture_schema()


# To satisfy Yua's requirement 5:
# "Name the source-side schema/model boundary and validation invariants: non-empty finite query vector; at least two docs; unique non-empty ids; finite document vectors with dimension equal to query vector; integer relevance labels; unknown/missing fields fail closed.
# Add focused red/control cases for dimension mismatch and non-finite values, or explicitly narrow the claim from typed to shape-validated. Do not leave the word "typed" backed only by a dict literal."

# We will define tests for these schema invariants. We can create a dummy schema parser in the test file that implements what we WANT the source to do, and assert that the FUTURE source schema does this.
# Since we can't test the future schema, we can write the tests against a dummy parser to PROVE the discrimination works, and later swap it for the real one. Or we can just test the real one and mark it xfail.


def _get_smoke_fixture_schema() -> Any:
    try:
        import musubi.evals.schema

        if hasattr(musubi.evals.schema, "SmokeFixture"):
            return getattr(musubi.evals.schema, "SmokeFixture")
        raise DefectStillPresent("SmokeFixture schema is not yet implemented")
    except ImportError:
        raise DefectStillPresent("SmokeFixture schema is not yet implemented")


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: SmokeFixture not implemented"
)
def test_schema_valid_fixture() -> None:
    SmokeFixture = _get_smoke_fixture_schema()
    payload = {
        "query_embedding": [0.1, 0.2],
        "corpus": [
            {"id": "d1", "text": "A", "relevance": 1, "embedding": [0.1, 0.2]},
            {"id": "d2", "text": "B", "relevance": 0, "embedding": [0.3, 0.4]},
        ],
    }
    obj = SmokeFixture.model_validate(payload)
    assert len(obj.corpus) == 2


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: SmokeFixture not implemented"
)
def test_schema_invalid_dimension_mismatch() -> None:
    SmokeFixture = _get_smoke_fixture_schema()
    payload = {
        "query_embedding": [0.1, 0.2],
        "corpus": [
            {"id": "d1", "text": "A", "relevance": 1, "embedding": [0.1, 0.2, 0.3]},  # Mismatch
            {"id": "d2", "text": "B", "relevance": 0, "embedding": [0.3, 0.4]},
        ],
    }
    with pytest.raises(ValueError):  # Or ValidationError
        SmokeFixture.model_validate(payload)


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: SmokeFixture not implemented"
)
def test_schema_invalid_non_finite() -> None:
    SmokeFixture = _get_smoke_fixture_schema()
    import math

    payload = {
        "query_embedding": [math.inf, 0.2],
        "corpus": [
            {"id": "d1", "text": "A", "relevance": 1, "embedding": [0.1, 0.2]},
            {"id": "d2", "text": "B", "relevance": 0, "embedding": [0.3, 0.4]},
        ],
    }
    with pytest.raises(ValueError):
        SmokeFixture.model_validate(payload)


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: SmokeFixture not implemented"
)
def test_schema_invalid_not_enough_docs() -> None:
    SmokeFixture = _get_smoke_fixture_schema()
    payload = {
        "query_embedding": [0.1, 0.2],
        "corpus": [{"id": "d1", "text": "A", "relevance": 1, "embedding": [0.1, 0.2]}],
    }
    with pytest.raises(ValueError):
        SmokeFixture.model_validate(payload)


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: SmokeFixture not implemented"
)
def test_schema_invalid_duplicate_ids() -> None:
    SmokeFixture = _get_smoke_fixture_schema()
    payload = {
        "query_embedding": [0.1, 0.2],
        "corpus": [
            {"id": "d1", "text": "A", "relevance": 1, "embedding": [0.1, 0.2]},
            {"id": "d1", "text": "B", "relevance": 0, "embedding": [0.3, 0.4]},
        ],
    }
    with pytest.raises(ValueError):
        SmokeFixture.model_validate(payload)


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: SmokeFixture not implemented"
)
def test_schema_invalid_extra_fields() -> None:
    SmokeFixture = _get_smoke_fixture_schema()
    payload = {
        "query_embedding": [0.1, 0.2],
        "corpus": [
            {"id": "d1", "text": "A", "relevance": 1, "embedding": [0.1, 0.2]},
            {"id": "d2", "text": "B", "relevance": 0, "embedding": [0.3, 0.4]},
        ],
        "unknown_field": True,
    }
    with pytest.raises(ValueError):
        SmokeFixture.model_validate(payload)
