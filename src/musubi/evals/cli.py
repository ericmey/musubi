import argparse
import json
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from musubi.evals.corpus import verify_manifest
from musubi.evals.runner import run_smoke_gate
from musubi.evals.schema import GoldenQuery, SmokeFixture


def _write(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def main() -> None:
    parser = argparse.ArgumentParser(prog="musubi-evals")
    parser.add_argument("command", choices=["smoke", "scheduled"])
    parser.add_argument("--data-dir", default="tests/evals/data", type=Path)
    args = parser.parse_args()

    data_dir = args.data_dir
    manifest_path = data_dir / "manifest.json"
    corpus_path = data_dir / "corpus.yaml"
    smoke_fixture_path = data_dir / "smoke_fixture.json"
    baseline_path = data_dir / "baseline.json"

    required_input = smoke_fixture_path if args.command == "smoke" else corpus_path
    if not manifest_path.exists() or not required_input.exists():
        _write("Missing corpus or manifest.")
        raise SystemExit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)

    # Validate manifest and checksum
    try:
        verify_manifest(manifest, data_dir)
    except ValueError as e:
        _write(f"Manifest verification failed: {e}")
        raise SystemExit(1) from e

    if args.command == "smoke":
        try:
            fixture = SmokeFixture.model_validate_json(smoke_fixture_path.read_text())
        except (OSError, ValidationError, ValueError) as exc:
            _write(f"Schema validation failed: {exc}")
            raise SystemExit(1) from exc

        result = run_smoke_gate(
            [document.model_dump(mode="json") for document in fixture.corpus],
            query_embedding=list(fixture.query_embedding),
        )
        ndcg = result.metrics.get("ndcg@10", 0.0)
        if ndcg < 0.5:
            _write(f"Smoke gate failed threshold: ndcg@10={ndcg} < 0.5")
            raise SystemExit(1)
        if baseline_path.exists():
            with open(baseline_path) as f:
                baseline = json.load(f)
            if ndcg < baseline.get("ndcg@10", 0.0) - 0.02:
                _write(f"Regression detected: ndcg@10={ndcg} vs baseline {baseline.get('ndcg@10')}")
                raise SystemExit(1)
        _write(f"Smoke gate passed. ndcg@10={ndcg}")
        raise SystemExit(0)

    corpus: list[dict[str, object]] = []
    try:
        with open(corpus_path) as f:
            docs = list(yaml.safe_load_all(f))
        for doc in docs:
            # We assume GoldenQuery schema
            if doc:
                q = GoldenQuery.model_validate(doc)
                # Need to convert to runner shape for smoke gate
                # The smoke gate expects dicts
                # But here we just prove schema is validated
                # Let's rebuild a simple mock dictionary for run_smoke_gate
                corpus.append({"id": q.id, "text": q.text, "relevance": 1 if q.relevant else 0})
    except (OSError, yaml.YAMLError, ValidationError, ValueError, TypeError) as exc:
        _write(f"Schema validation failed: {exc}")
        raise SystemExit(1) from exc

    if args.command == "scheduled":
        _write("Scheduled live Qdrant+TEI quality gate is not implemented; see Issue #430.")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
