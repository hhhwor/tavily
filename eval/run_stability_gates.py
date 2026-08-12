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
from eval.research_quality_gate import (
    DEFAULT_BASELINE as DEFAULT_RESEARCH_BASELINE,
    DEFAULT_CORPUS as DEFAULT_RESEARCH_CORPUS,
    DEFAULT_REPORT as DEFAULT_RESEARCH_REPORT,
    run_gate as run_research_quality_gate,
)


def run_all(
    *,
    corpus_path: Path = DEFAULT_CORPUS,
    baseline_path: Path = DEFAULT_BASELINE,
    quality_report_path: Path | None = DEFAULT_QUALITY_REPORT,
    research_corpus_path: Path = DEFAULT_RESEARCH_CORPUS,
    research_baseline_path: Path = DEFAULT_RESEARCH_BASELINE,
    research_report_path: Path | None = DEFAULT_RESEARCH_REPORT,
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
    research_quality = None
    try:
        research_quality = run_research_quality_gate(
            corpus_path=research_corpus_path,
            baseline_path=research_baseline_path,
            report_path=research_report_path,
        )
    except Exception as exc:
        failures.append(f"research_quality: {exc}")

    result = {
        "status": "failed" if failures else "passed",
        "quality": {
            "status": quality["status"],
            "query_count": quality["query_count"],
            "metrics": quality["metrics"]["overall"],
        } if quality is not None else None,
        "research_quality": {
            "status": research_quality["status"],
            "case_count": research_quality["case_count"],
            "metrics": research_quality["metrics"]["overall"],
        } if research_quality is not None else None,
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
    parser.add_argument(
        "--research-corpus", type=Path, default=DEFAULT_RESEARCH_CORPUS
    )
    parser.add_argument(
        "--research-baseline", type=Path, default=DEFAULT_RESEARCH_BASELINE
    )
    parser.add_argument(
        "--research-report", type=Path, default=DEFAULT_RESEARCH_REPORT
    )
    args = parser.parse_args()
    result = run_all(
        corpus_path=args.corpus,
        baseline_path=args.baseline,
        quality_report_path=args.quality_report,
        research_corpus_path=args.research_corpus,
        research_baseline_path=args.research_baseline,
        research_report_path=args.research_report,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
