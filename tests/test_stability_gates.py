"""Regression tests for deterministic quality and concurrency gates."""
from __future__ import annotations

import copy
import json

from eval.concurrency_gate import (
    DEFAULT_THRESHOLDS,
    compare_level,
    run_gate as run_concurrency_gate,
)
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


def test_concurrency_gate_runs_both_required_levels():
    report = run_concurrency_gate(
        levels=(20, 50),
        requests_per_worker=1,
        report_path=None,
    )

    assert report["status"] == "passed"
    assert set(report["levels"]) == {"20", "50"}
    for result in report["levels"].values():
        assert result["success_rate"] == 1.0
        assert result["retry_recovery_rate"] == 1.0
        assert result["isolation"]["recall_pool_bound_respected"]
        assert result["isolation"]["ranking_pool_bound_respected"]


def test_concurrency_gate_detects_latency_regression():
    report = run_concurrency_gate(
        levels=(20,),
        requests_per_worker=1,
        report_path=None,
    )
    result = copy.deepcopy(report["levels"]["20"])
    thresholds = json.loads(
        DEFAULT_THRESHOLDS.read_text(encoding="utf-8")
    )["levels"]["20"]
    result["latency_ms"]["p95"] = thresholds["max_p95_ms"] + 1

    failures = compare_level(result, thresholds)

    assert any("latency_ms.p95" in failure for failure in failures)
