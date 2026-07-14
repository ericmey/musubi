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

    assert passed_corpus == expected_fixture["corpus"], (
        "CLI must pass the verbatim corpus without adding, removing, or transforming fields"
    )


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
    _assert_verbatim_projection(runner_calls, fixture)


@pytest.mark.xfail(
    strict=True,
    raises=DefectStillPresent,
    reason="RET-004: CLI schema validation drops unlisted fields",
)
def test_eval_cli_seam_invalid_dimension_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    fixture = {
        "query_embedding": [0.1, 0.2],
        "corpus": [
            {"id": "d1", "text": "A", "relevance": 1, "embedding": [0.1, 0.2, 0.3]},  # Mismatch
            {"id": "d2", "text": "B", "relevance": 0, "embedding": [0.3, 0.4]},
        ],
    }
    _write_fixture(data_dir, fixture)

    # Touch corpus.yaml so it bypasses the legacy existence check
    (data_dir / "corpus.yaml").touch()

    # This must fail closed due to schema validation BEFORE invoking the runner
    monkeypatch.setattr(sys, "argv", ["musubi-evals", "smoke", "--data-dir", str(data_dir)])
    try:
        cli.main()
    except SystemExit as exc:
        if exc.code == 0:
            raise DefectStillPresent("CLI schema validation allows dimension mismatch")
        out, err = capsys.readouterr()
        # Ensure it failed for the *expected validation* reason
        if "Schema validation failed" not in out:
            raise AssertionError(f"CLI exited with {exc.code} for unrelated reason: {out} {err}")
    except Exception as e:
        raise AssertionError(f"CLI raised unexpected exception: {e}")


@pytest.mark.xfail(
    strict=True,
    raises=DefectStillPresent,
    reason="RET-004: CLI schema validation ignores unknown nested fields",
)
def test_eval_cli_seam_invalid_unknown_field_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    fixture = {
        "query_embedding": [0.1, 0.2],
        "corpus": [
            {"id": "d1", "text": "A", "relevance": 1, "embedding": [0.1, 0.2], "unknown": 42},
            {"id": "d2", "text": "B", "relevance": 0, "embedding": [0.3, 0.4]},
        ],
    }
    _write_fixture(data_dir, fixture)

    (data_dir / "corpus.yaml").touch()

    monkeypatch.setattr(sys, "argv", ["musubi-evals", "smoke", "--data-dir", str(data_dir)])
    try:
        cli.main()
    except SystemExit as exc:
        if exc.code == 0:
            raise DefectStillPresent("CLI schema validation allows unknown nested document fields")
        out, err = capsys.readouterr()
        if "Schema validation failed" not in out:
            raise AssertionError(f"CLI exited with {exc.code} for unrelated reason: {out} {err}")
    except Exception as e:
        raise AssertionError(f"CLI raised unexpected exception: {e}")


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
    with pytest.raises(AssertionError, match="verbatim corpus"):
        _assert_verbatim_projection(calls, fixture)


def test_discrimination_bypassed_runner() -> None:
    fixture = {
        "query_embedding": [0.1],
        "corpus": [{"id": "1", "text": "A", "relevance": 1, "embedding": [0.1]}],
    }
    calls: list[dict[str, Any]] = []  # Bypassed
    with pytest.raises(AssertionError, match="CLI must invoke run_smoke_gate exactly once"):
        _assert_verbatim_projection(calls, fixture)


def test_discrimination_modified_corpus_fails() -> None:
    fixture = {
        "query_embedding": [0.1],
        "corpus": [{"id": "1", "text": "A", "relevance": 1, "embedding": [0.1]}],
    }
    modified_corpus = [
        {"id": "1", "text": "A", "relevance": 1, "embedding": [0.1], "extra": "data"}
    ]
    calls = [{"corpus": modified_corpus, "query_embedding": [0.1]}]
    with pytest.raises(
        AssertionError,
        match="CLI must pass the verbatim corpus without adding, removing, or transforming fields",
    ):
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
    assert obj.model_dump(mode="json") == payload


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
@pytest.mark.parametrize("bad_val", [float("inf"), float("-inf"), float("nan"), "0.1"])
def test_schema_invalid_non_finite_query_vector(bad_val: Any) -> None:
    SmokeFixture = _get_smoke_fixture_schema()

    payload = {
        "query_embedding": [bad_val, 0.2],
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
@pytest.mark.parametrize("bad_val", [float("inf"), float("-inf"), float("nan"), "0.1"])
def test_schema_invalid_non_finite_doc_vector(bad_val: Any) -> None:
    SmokeFixture = _get_smoke_fixture_schema()

    payload = {
        "query_embedding": [0.1, 0.2],
        "corpus": [
            {"id": "d1", "text": "A", "relevance": 1, "embedding": [bad_val, 0.2]},
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


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: SmokeFixture not implemented"
)
def test_schema_invalid_empty_query_vector() -> None:
    SmokeFixture = _get_smoke_fixture_schema()
    payload = {
        "query_embedding": [],
        "corpus": [
            {"id": "d1", "text": "A", "relevance": 1, "embedding": []},
            {"id": "d2", "text": "B", "relevance": 0, "embedding": []},
        ],
    }
    with pytest.raises(ValueError):
        SmokeFixture.model_validate(payload)


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: SmokeFixture not implemented"
)
def test_schema_invalid_empty_id() -> None:
    SmokeFixture = _get_smoke_fixture_schema()
    payload = {
        "query_embedding": [0.1, 0.2],
        "corpus": [
            {"id": "", "text": "A", "relevance": 1, "embedding": [0.1, 0.2]},
            {"id": "d2", "text": "B", "relevance": 0, "embedding": [0.3, 0.4]},
        ],
    }
    with pytest.raises(ValueError):
        SmokeFixture.model_validate(payload)


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: SmokeFixture not implemented"
)
@pytest.mark.parametrize("bad_relevance", [1.5, True, "1"])
def test_schema_invalid_non_integer_relevance(bad_relevance: Any) -> None:
    SmokeFixture = _get_smoke_fixture_schema()
    payload = {
        "query_embedding": [0.1, 0.2],
        "corpus": [
            {"id": "d1", "text": "A", "relevance": bad_relevance, "embedding": [0.1, 0.2]},
            {"id": "d2", "text": "B", "relevance": 0, "embedding": [0.3, 0.4]},
        ],
    }
    with pytest.raises(ValueError):
        SmokeFixture.model_validate(payload)


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: SmokeFixture not implemented"
)
def test_schema_invalid_missing_required_field() -> None:
    SmokeFixture = _get_smoke_fixture_schema()
    payload = {
        "query_embedding": [0.1, 0.2],
        "corpus": [
            {"id": "d1", "relevance": 1, "embedding": [0.1, 0.2]},  # Missing text
            {"id": "d2", "text": "B", "relevance": 0, "embedding": [0.3, 0.4]},
        ],
    }
    with pytest.raises(ValueError):
        SmokeFixture.model_validate(payload)


@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="RET-004: SmokeFixture not implemented"
)
def test_schema_invalid_unknown_doc_field() -> None:
    SmokeFixture = _get_smoke_fixture_schema()
    payload = {
        "query_embedding": [0.1, 0.2],
        "corpus": [
            {"id": "d1", "text": "A", "relevance": 1, "embedding": [0.1, 0.2], "extra_field": 123},
            {"id": "d2", "text": "B", "relevance": 0, "embedding": [0.3, 0.4]},
        ],
    }
    with pytest.raises(ValueError):
        SmokeFixture.model_validate(payload)
