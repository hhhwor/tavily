"""Evaluate a frozen embedding intent artifact without recalibrating it."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from eval.intent_route_corpus import SOURCE_ORDER, group_counts, holdout_cases
from eval.train_embedding_intent_classifier import (
    DEFAULT_BASE_URL,
    DEFAULT_OUTPUT,
    _load_local_env,
    _sigmoid,
    _targets,
    evaluate,
    fetch_embeddings,
)
from src.l0 import plan_query


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--embedding-cache", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--show-errors", action="store_true")
    args = parser.parse_args()

    artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
    if artifact.get("source_order") != list(SOURCE_ORDER):
        raise SystemExit("artifact source_order does not match evaluation corpus")
    env = _load_local_env()
    api_key = env.get("SILICONFLOW_API_KEY", "")
    if not api_key:
        raise SystemExit("SILICONFLOW_API_KEY is required")
    cases = holdout_cases()
    features = fetch_embeddings(
        cases,
        api_key=api_key,
        base_url=env.get("SILICONFLOW_BASE_URL", DEFAULT_BASE_URL),
        model=artifact["embedding_model"],
        batch_size=32,
        timeout=60.0,
        cache_path=args.embedding_cache,
    )
    weights = np.asarray([
        artifact["weights"][source] for source in SOURCE_ORDER
    ]).T
    bias = np.asarray([artifact["bias"][source] for source in SOURCE_ORDER])
    thresholds = [
        artifact["thresholds"][source] for source in SOURCE_ORDER
    ]
    probabilities = _sigmoid(features @ weights + bias)
    metrics = evaluate(cases, _targets(cases), probabilities, thresholds)
    predicted = probabilities >= np.asarray(thresholds)
    effective = predicted.copy()
    for index, case in enumerate(cases):
        rule_plan = plan_query(
            case.query,
            ["web"],
            10,
            rewrite=False,
            academic_detect=True,
            patent_detect=True,
            legal_detect=True,
        )
        rule_sources = np.asarray((
            rule_plan.academic,
            rule_plan.patent,
            rule_plan.legal,
        ))
        if int(rule_sources.sum()) >= 2:
            effective[index] = np.logical_or(rule_sources, predicted[index])
    planner_metrics = evaluate(
        cases,
        _targets(cases),
        effective.astype(np.float64),
        (0.5, 0.5, 0.5),
    )
    result = {
        "embedding_model": artifact["embedding_model"],
        "artifact_version": artifact["artifact_version"],
        "corpus_sha256": artifact["training"]["corpus_sha256"],
        "thresholds": artifact["thresholds"],
        "holdout_group_counts": group_counts(cases),
        "metrics": metrics,
        "planner_effective_metrics": planner_metrics,
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")

    if args.show_errors:
        truth = _targets(cases) > 0.5
        for case, expected, actual, scores in zip(
            cases, truth, predicted, probabilities
        ):
            if np.array_equal(expected, actual):
                continue
            actual_sources = tuple(
                source for source, selected in zip(SOURCE_ORDER, actual)
                if selected
            )
            print(json.dumps({
                "case_id": case.case_id,
                "query": case.query,
                "expected": case.source_types,
                "actual": actual_sources,
                "scores": {
                    source: round(float(score), 6)
                    for source, score in zip(SOURCE_ORDER, scores)
                },
            }, ensure_ascii=False))


if __name__ == "__main__":
    main()
