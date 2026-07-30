"""Run all deterministic merge gates with one command."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.concurrency_gate import (
    DEFAULT_LEVELS,
    DEFAULT_REPORT as DEFAULT_CONCURRENCY_REPORT,
    DEFAULT_THRESHOLDS,
    parse_levels,
    run_gate as run_concurrency_gate,
)
from eval.quality_golden_gate import (
    DEFAULT_BASELINE,
    DEFAULT_CORPUS,
    DEFAULT_REPORT as DEFAULT_QUALITY_REPORT,
    run_gate as run_quality_gate,
)


def run_all(
    *,
    corpus_path: Path = DEFAULT_CORPUS,
    baseline_path: Path = DEFAULT_BASELINE,
    quality_report_path: Path | None = DEFAULT_QUALITY_REPORT,
    levels: tuple[int, ...] = DEFAULT_LEVELS,
    requests_per_worker: int = 2,
    thresholds_path: Path = DEFAULT_THRESHOLDS,
    concurrency_report_path: Path | None = DEFAULT_CONCURRENCY_REPORT,
) -> dict:
    failures: list[str] = []
    quality = None
    concurrency = None
    try:
        quality = run_quality_gate(
            corpus_path=corpus_path,
            baseline_path=baseline_path,
            report_path=quality_report_path,
        )
    except Exception as exc:
        failures.append(f"quality: {exc}")
    try:
        concurrency = run_concurrency_gate(
            levels=levels,
            requests_per_worker=requests_per_worker,
            thresholds_path=thresholds_path,
            report_path=concurrency_report_path,
        )
    except Exception as exc:
        failures.append(f"concurrency: {exc}")

    result = {
        "status": "failed" if failures else "passed",
        "quality": {
            "status": quality["status"],
            "query_count": quality["query_count"],
            "metrics": quality["metrics"]["overall"],
        } if quality is not None else None,
        "concurrency": {
            "status": concurrency["status"],
            "levels": {
                level: {
                    "request_count": row["request_count"],
                    "p95_ms": row["latency_ms"]["p95"],
                    "throughput_rps": row["throughput_rps"],
                    "success_rate": row["success_rate"],
                }
                for level, row in concurrency["levels"].items()
            },
        } if concurrency is not None else None,
        "failures": failures,
    }
    if failures:
        raise RuntimeError(
            "stability gates failed: " + "; ".join(failures)
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--quality-report",
        type=Path,
        default=DEFAULT_QUALITY_REPORT,
    )
    parser.add_argument("--levels", type=parse_levels, default=DEFAULT_LEVELS)
    parser.add_argument("--requests-per-worker", type=int, default=2)
    parser.add_argument("--thresholds", type=Path, default=DEFAULT_THRESHOLDS)
    parser.add_argument(
        "--concurrency-report",
        type=Path,
        default=DEFAULT_CONCURRENCY_REPORT,
    )
    args = parser.parse_args()
    result = run_all(
        corpus_path=args.corpus,
        baseline_path=args.baseline,
        quality_report_path=args.quality_report,
        levels=args.levels,
        requests_per_worker=args.requests_per_worker,
        thresholds_path=args.thresholds,
        concurrency_report_path=args.concurrency_report,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
