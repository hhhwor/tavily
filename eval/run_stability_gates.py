"""Run the deterministic quality merge gate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

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
) -> dict:
    failures: list[str] = []
    quality = None
    try:
        quality = run_quality_gate(
            corpus_path=corpus_path,
            baseline_path=baseline_path,
            report_path=quality_report_path,
        )
    except Exception as exc:
        failures.append(f"quality: {exc}")

    result = {
        "status": "failed" if failures else "passed",
        "quality": {
            "status": quality["status"],
            "query_count": quality["query_count"],
            "metrics": quality["metrics"]["overall"],
        } if quality is not None else None,
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
    args = parser.parse_args()
    result = run_all(
        corpus_path=args.corpus,
        baseline_path=args.baseline,
        quality_report_path=args.quality_report,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
