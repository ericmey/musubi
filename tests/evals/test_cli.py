import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from musubi.evals import cli


@pytest.mark.xfail(
    strict=True, reason="RET-004: CLI omits embeddings and strips document embeddings before runner"
)
def test_eval_cli_seam_fixed_embeddings_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Contract: The CLI must read the fixed-embedding PR-smoke fixture
    (containing query_embedding and document embeddings), and pass them
    verbatim into run_smoke_gate without stripping or hardcoding.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # The required fixed-embedding fixture shape
    fixture = {
        "query_embedding": [0.1, 0.2, 0.3],
        "corpus": [
            {"id": "doc1", "text": "alpha", "relevance": 3, "embedding": [0.1, 0.2, 0.3]},
            {"id": "doc2", "text": "beta", "relevance": 0, "embedding": [0.9, -0.1, 0.0]},
        ],
    }

    fixture_path = data_dir / "smoke_fixture.json"
    with open(fixture_path, "w") as f:
        json.dump(fixture, f)

    import hashlib

    with open(fixture_path, "rb") as bf:
        checksum = hashlib.sha256(bf.read()).hexdigest()

    manifest = {"name": "smoke_fixture", "files": {"smoke_fixture.json": checksum}}
    with open(data_dir / "manifest.json", "w") as f:
        json.dump(manifest, f)

    # Baseline to pass
    baseline = {"ndcg@10": 0.0}
    with open(data_dir / "baseline.json", "w") as f:
        json.dump(baseline, f)

    # Patch sys.argv
    monkeypatch.setattr(sys, "argv", ["musubi-evals", "smoke", "--data-dir", str(data_dir)])

    # Spy on run_smoke_gate
    runner_calls: list[dict[str, Any]] = []

    def mock_run_smoke_gate(
        corpus: list[dict[str, Any]], query_embedding: list[float] | None = None
    ) -> Any:
        runner_calls.append({"corpus": corpus, "query_embedding": query_embedding})
        return type("MockResult", (), {"metrics": {"ndcg@10": 1.0}})()

    monkeypatch.setattr(cli, "run_smoke_gate", mock_run_smoke_gate)

    # We expect a SystemExit(0) on success
    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0, "CLI must succeed when baseline is met"
    assert len(runner_calls) == 1, "CLI must invoke run_smoke_gate exactly once"

    call = runner_calls[0]
    passed_corpus = call["corpus"]
    passed_query_embedding = call.get("query_embedding")

    # Assert query_embedding is present and matches the fixture
    assert passed_query_embedding is not None, "CLI must pass query_embedding to the runner"
    assert passed_query_embedding == [0.1, 0.2, 0.3], "CLI must pass the exact query_embedding"

    # Assert document embeddings are preserved, not stripped
    assert len(passed_corpus) == 2, "Corpus must have 2 documents"
    for doc in passed_corpus:
        assert "embedding" in doc, "CLI must not strip document embeddings"

    assert passed_corpus[0]["embedding"] == [0.1, 0.2, 0.3]
    assert passed_corpus[1]["embedding"] == [0.9, -0.1, 0.0]


def test_workflow_fails_on_regression(tmp_path: Path) -> None:
    # Setup data
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Create fake corpus
    corpus = {
        "id": "q001",
        "text": "healthy query",
        "relevant": [{"object_id": "1", "relevance": 3}],
        "mode": "fast",
        "namespace": "test/ns",
    }

    corpus_path = data_dir / "corpus.yaml"
    with open(corpus_path, "w") as f:
        yaml.safe_dump(corpus, f)

    import hashlib

    with open(corpus_path, "rb") as f:
        checksum = hashlib.sha256(f.read()).hexdigest()

    manifest = {"name": "test_corpus", "files": {"corpus.yaml": checksum}}
    with open(data_dir / "manifest.json", "w") as f:
        json.dump(manifest, f)

    # Create baseline with impossibly high ndcg@10 to force a regression
    baseline = {"ndcg@10": 1.5, "mrr": 1.0, "latency_p95_ms": 100.0}
    with open(data_dir / "baseline.json", "w") as f:
        json.dump(baseline, f)

    # Run the CLI
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "musubi.evals", "smoke", "--data-dir", str(data_dir)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Regression detected" in result.stdout


def test_workflow_passes_on_success(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    corpus = {
        "id": "q001",
        "text": "healthy query",
        "relevant": [{"object_id": "1", "relevance": 3}],
        "mode": "fast",
        "namespace": "test/ns",
    }

    corpus_path = data_dir / "corpus.yaml"
    with open(corpus_path, "w") as f:
        yaml.safe_dump(corpus, f)

    import hashlib

    with open(corpus_path, "rb") as f:
        checksum = hashlib.sha256(f.read()).hexdigest()

    manifest = {"name": "test_corpus", "files": {"corpus.yaml": checksum}}
    with open(data_dir / "manifest.json", "w") as f:
        json.dump(manifest, f)

    # Create baseline with low ndcg to pass
    baseline = {"ndcg@10": 0.0, "mrr": 0.0, "latency_p95_ms": 1000.0}
    with open(data_dir / "baseline.json", "w") as f:
        json.dump(baseline, f)

    import sys

    result = subprocess.run(
        [sys.executable, "-m", "musubi.evals", "smoke", "--data-dir", str(data_dir)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Smoke gate passed" in result.stdout
