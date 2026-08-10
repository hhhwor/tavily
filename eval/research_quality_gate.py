"""Offline gate for the reviewed M3 Research policy corpus."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from src.application.research_dossier_builder import dossier_decision


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "eval" / "golden" / "research_quality_corpus.json"
DEFAULT_BASELINE = ROOT / "eval" / "golden" / "research_quality_baseline.json"
DEFAULT_REPORT = ROOT / "eval" / "golden" / "research_quality_report.json"
RUNNER_VERSION = "research-quality.v1"
PROFILES = {
    "literature_review",
    "technology_validation",
    "prior_art_landscape",
    "technology_landscape",
}
METRICS = (
    "status_accuracy",
    "claim_support_precision",
    "locator_validity",
    "identity_accuracy",
    "counterevidence_coverage",
    "conflict_disclosure_recall",
    "gap_disclosure_recall",
    "unsupported_statement_rate",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_corpus(path: Path = DEFAULT_CORPUS) -> dict[str, Any]:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    if corpus.get("schema_version") != "research-quality-corpus.v1":
        raise ValueError("unsupported Research corpus version")
    cases = corpus.get("cases")
    if not isinstance(cases, list) or len(cases) < 50:
        raise ValueError("Research corpus must contain at least 50 cases")
    ids = [item.get("id") for item in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("Research corpus case IDs must be unique")
    profile_counts = {
        profile: sum(item.get("profile") == profile for item in cases)
        for profile in PROFILES
    }
    missing = [
        profile for profile, count in profile_counts.items() if count < 10
    ]
    if missing:
        raise ValueError(
            "each Research profile needs at least 10 cases: "
            + ", ".join(sorted(missing))
        )
    return corpus


def _predict(case: dict[str, Any]) -> dict[str, Any]:
    scenario = case["scenario"]
    support = int(scenario["qualified_support"])
    conflict = int(scenario["qualified_conflict"])
    if support > 0 and conflict > 0:
        status = "conflicted"
    elif support > 0:
        status = "supported"
    else:
        status = "insufficient"
    decision = dossier_decision(
        status,
        has_qualified_support=support > 0,
    )
    return {
        "status": status,
        "statement_kind": decision.kind,
        "conflict_disclosed": decision.disclose_conflict,
        "gap_disclosed": decision.disclose_gap,
        "independent_work_count": len(set(
            scenario["independent_work_ids"]
        )),
    }


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _metrics(cases: list[dict[str, Any]]) -> dict[str, float]:
    predictions = [_predict(case) for case in cases]
    labels = [case["label"] for case in cases]
    factual = [
        index for index, row in enumerate(predictions)
        if row["statement_kind"] == "factual"
    ]
    conflicts = [
        index for index, row in enumerate(labels)
        if row["conflict_disclosed"]
    ]
    gaps = [
        index for index, row in enumerate(labels) if row["gap_disclosed"]
    ]
    qualified = sum(
        int(case["scenario"]["qualified_support"])
        + int(case["scenario"]["qualified_conflict"])
        for case in cases
    )
    resolved = sum(
        int(case["scenario"]["resolvable_locators"])
        for case in cases
    )
    unsupported = sum(
        int(cases[index]["scenario"]["qualified_support"]) <= 0
        or int(cases[index]["scenario"]["resolvable_locators"]) <= 0
        for index in factual
    )
    return {
        "status_accuracy": _ratio(sum(
            predicted["status"] == label["status"]
            for predicted, label in zip(predictions, labels)
        ), len(cases)),
        "claim_support_precision": _ratio(sum(
            labels[index]["status"] == "supported" for index in factual
        ), len(factual)),
        "locator_validity": _ratio(resolved, qualified),
        "identity_accuracy": _ratio(sum(
            predicted["independent_work_count"]
            == label["independent_work_count"]
            for predicted, label in zip(predictions, labels)
        ), len(cases)),
        "counterevidence_coverage": _ratio(sum(
            bool(case["scenario"]["counterevidence_searched"])
            for case in cases
        ), len(cases)),
        "conflict_disclosure_recall": _ratio(sum(
            predictions[index]["conflict_disclosed"] for index in conflicts
        ), len(conflicts)),
        "gap_disclosure_recall": _ratio(sum(
            predictions[index]["gap_disclosed"] for index in gaps
        ), len(gaps)),
        "unsupported_statement_rate": _ratio(unsupported, len(factual)),
    }


def evaluate_corpus(corpus: dict[str, Any]) -> dict[str, Any]:
    cases = list(corpus["cases"])
    by_profile = {
        profile: [case for case in cases if case["profile"] == profile]
        for profile in sorted(PROFILES)
    }
    return {
        "runner_version": RUNNER_VERSION,
        "case_count": len(cases),
        "metrics": {
            "overall": _metrics(cases),
            **{
                profile: _metrics(rows)
                for profile, rows in by_profile.items()
            },
        },
        "profile_case_counts": {
            profile: len(rows) for profile, rows in by_profile.items()
        },
    }


def compare_to_baseline(
    actual: dict[str, Any],
    baseline: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    if actual.get("runner_version") != baseline.get("runner_version"):
        failures.append("runner_version mismatch")
    for profile, expected in baseline.get("metrics", {}).items():
        observed = actual.get("metrics", {}).get(profile)
        if observed is None:
            failures.append(f"missing profile: {profile}")
            continue
        for metric in METRICS:
            expected_value = float(expected[metric])
            observed_value = float(observed[metric])
            tolerance = float(baseline.get("max_drop", {}).get(metric, 0))
            if metric == "unsupported_statement_rate":
                if observed_value > expected_value + tolerance + 1e-12:
                    failures.append(
                        f"{profile}.{metric}={observed_value:.6f} "
                        f"above ceiling={expected_value + tolerance:.6f}"
                    )
            elif observed_value + 1e-12 < expected_value - tolerance:
                failures.append(
                    f"{profile}.{metric}={observed_value:.6f} "
                    f"below floor={expected_value - tolerance:.6f}"
                )
    hard = actual["metrics"]["overall"]
    hard_requirements = {
        "locator_validity": 1.0,
        "conflict_disclosure_recall": 1.0,
        "gap_disclosure_recall": 1.0,
        "counterevidence_coverage": 1.0,
    }
    for metric, floor in hard_requirements.items():
        if hard[metric] + 1e-12 < floor:
            failures.append(f"hard requirement failed: {metric}")
    if hard["unsupported_statement_rate"] > 0:
        failures.append("hard requirement failed: unsupported_statement_rate")
    return list(dict.fromkeys(failures))


def run_gate(
    *,
    corpus_path: Path = DEFAULT_CORPUS,
    baseline_path: Path = DEFAULT_BASELINE,
    report_path: Path | None = DEFAULT_REPORT,
    update_baseline: bool = False,
) -> dict[str, Any]:
    corpus = load_corpus(corpus_path)
    result = evaluate_corpus(corpus)
    result["corpus_sha256"] = _sha256(corpus_path)
    if update_baseline:
        baseline = {
            "schema_version": "research-quality-baseline.v1",
            "runner_version": RUNNER_VERSION,
            "corpus_sha256": result["corpus_sha256"],
            "case_count": result["case_count"],
            "max_drop": {metric: 0.0 for metric in METRICS},
            "metrics": result["metrics"],
        }
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        if baseline.get("corpus_sha256") != result["corpus_sha256"]:
            raise RuntimeError(
                "Research corpus hash changed; review it and run "
                "--update-baseline explicitly"
            )
    failures = compare_to_baseline(result, baseline)
    result["status"] = "failed" if failures else "passed"
    result["failures"] = failures
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if failures:
        raise RuntimeError("Research quality gate failed: " + "; ".join(failures))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()
    result = run_gate(
        corpus_path=args.corpus,
        baseline_path=args.baseline,
        report_path=args.report,
        update_baseline=args.update_baseline,
    )
    print(json.dumps({
        "status": result["status"],
        "case_count": result["case_count"],
        "metrics": result["metrics"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
