"""Regression tests for the deterministic quality gate."""
from __future__ import annotations

import copy
import json

from eval import concurrency_gate
from eval.quality_golden_gate import (
    DEFAULT_BASELINE,
    compare_to_baseline,
    run_gate as run_quality_gate,
)


def test_quality_golden_gate_matches_reviewed_baseline():
    result = run_quality_gate(report_path=None)

    assert result["status"] == "passed"
    assert result["query_count"] == 9
    assert set(result["metrics"]) == {
        "overall",
        "web",
        "academic",
        "patent",
    }


def test_quality_golden_gate_detects_metric_regression():
    actual = run_quality_gate(report_path=None)
    baseline = json.loads(DEFAULT_BASELINE.read_text(encoding="utf-8"))
    regressed = copy.deepcopy(actual)
    regressed["metrics"]["web"]["ndcg_at_k"] -= 0.03

    failures = compare_to_baseline(regressed, baseline)

    assert any("web.ndcg_at_k" in failure for failure in failures)


def test_concurrency_benchmark_is_advisory(monkeypatch):
    monkeypatch.setattr(
        concurrency_gate,
        "run_level",
        lambda *args, **kwargs: {"request_count": 1},
    )

    report = concurrency_gate.run_benchmark(
        levels=(1,),
        requests_per_worker=1,
        report_path=None,
    )

    assert report["status"] == "observed"
    assert report["levels"]["1"]["advisories"] == [
        "no advisory reference for concurrency=1"
    ]
