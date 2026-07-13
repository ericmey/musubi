import json
import subprocess
from pathlib import Path

import yaml


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
