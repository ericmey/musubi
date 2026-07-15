import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

from pydantic import ValidationError

from musubi.evals.corpus import verify_manifest
from musubi.evals.live_gate import (
    LiveGateUnavailable,
    build_settings_backends,
    enforce_thresholds,
)
from musubi.evals.runner import run_smoke_gate
from musubi.evals.schema import SmokeFixture


def _write(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def main() -> None:
    parser = argparse.ArgumentParser(prog="musubi-evals")
    parser.add_argument("command", choices=["smoke", "scheduled"])
    parser.add_argument("--data-dir", default="tests/evals/data", type=Path)
    args = parser.parse_args()

    data_dir = args.data_dir

    if args.command == "scheduled":
        _run_scheduled(data_dir)  # self-seeding live gate; raises SystemExit internally
        return

    manifest_path = data_dir / "manifest.json"
    smoke_fixture_path = data_dir / "smoke_fixture.json"
    baseline_path = data_dir / "baseline.json"

    required_input = smoke_fixture_path
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


def _run_scheduled(data_dir: Path) -> None:
    """The self-seeding scheduled live gate: seed the checksum-pinned graded corpus into a fresh
    run-scoped namespace via the production write seam, measure per-mode metrics, enforce the FROZEN
    thresholds, tear down the run data. Fail loud without TEI or on any seed/visibility/corpus error;
    on a threshold MISS, print the RAW per-mode results + corpus attribution — never tune to green."""
    from musubi.evals.scheduled_gate import (
        ScheduledGateFailure,
        new_run_id,
        run_scheduled_seeded_gate,
    )

    try:
        backends = build_settings_backends()
    except LiveGateUnavailable as exc:
        _write(f"Scheduled gate unavailable — fail-loud, no fabricated numbers: {exc}")
        raise SystemExit(3) from exc

    run_id = new_run_id()
    try:
        by_mode = asyncio.run(run_scheduled_seeded_gate(backends, data_dir=data_dir, run_id=run_id))
    except (LiveGateUnavailable, ScheduledGateFailure) as exc:
        _write(f"Scheduled gate failed (fail-loud, no fabricated numbers): {exc}")
        raise SystemExit(3) from exc

    try:
        enforce_thresholds(by_mode)
    except ValueError as exc:
        # Below threshold: report RAW per-mode results + corpus attribution, never tuned to green.
        _write(
            f"Scheduled gate BELOW thresholds (raw results, NOT tuned) — "
            f"corpus=scheduled_corpus.yaml run={run_id} metrics={by_mode} :: {exc}"
        )
        raise SystemExit(1) from exc
    _write(f"Scheduled gate PASSED — corpus=scheduled_corpus.yaml run={run_id} metrics={by_mode}")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
