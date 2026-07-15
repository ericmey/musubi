import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from musubi.evals.corpus import verify_manifest
from musubi.evals.live_gate import (
    LiveGateUnavailable,
    build_settings_retriever,
    enforce_thresholds,
    run_live_gate,
)
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
        _write(f"Missing {required_input.name} or manifest.json.")
        raise SystemExit(1)

    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        _write(f"Manifest loading failed: {exc}")
        raise SystemExit(1) from exc

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
            try:
                with open(baseline_path) as f:
                    baseline = json.load(f)
                baseline_ndcg = baseline.get("ndcg@10") if isinstance(baseline, dict) else None
                if (
                    isinstance(baseline_ndcg, bool)
                    or not isinstance(baseline_ndcg, (int, float))
                    or not math.isfinite(float(baseline_ndcg))
                ):
                    raise ValueError("ndcg@10 must be a finite number")
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                _write(f"Baseline validation failed: {exc}")
                raise SystemExit(1) from exc
            if ndcg < float(baseline_ndcg) - 0.02:
                _write(f"Regression detected: ndcg@10={ndcg} vs baseline {baseline_ndcg}")
                raise SystemExit(1)
        _write(f"Smoke gate passed. ndcg@10={ndcg}")
        raise SystemExit(0)

    golden_queries: list[GoldenQuery] = []
    try:
        with open(corpus_path) as f:
            docs = list(yaml.safe_load_all(f))
        for doc in docs:
            if doc:
                golden_queries.append(GoldenQuery.model_validate(doc))
    except (OSError, yaml.YAMLError, ValidationError, ValueError, TypeError) as exc:
        _write(f"Schema validation failed: {exc}")
        raise SystemExit(1) from exc

    if args.command == "scheduled":
        queries = [
            {
                "id": query.id,
                "text": query.text,
                "mode": query.mode,
                "namespace": query.namespace,
                "relevant": query.relevant,
            }
            for query in golden_queries
        ]
        try:
            retriever = build_settings_retriever()
            by_mode = asyncio.run(run_live_gate(queries, retriever))
            enforce_thresholds(by_mode)
        except LiveGateUnavailable as exc:
            # No TEI stack (or it dropped mid-run): FAIL LOUD — never a fabricated or empty pass.
            # Real quality numbers are proven on the scheduled x86 TEI CI, not on a TEI-less box.
            _write(f"Scheduled live gate unavailable — fail-loud, no fabricated numbers: {exc}")
            raise SystemExit(3) from exc
        except ValueError as exc:
            _write(f"Scheduled live gate FAILED quality thresholds: {exc}")
            raise SystemExit(1) from exc
        _write(f"Scheduled live gate passed all mode thresholds: {by_mode}")
        raise SystemExit(0)


if __name__ == "__main__":
    main()
